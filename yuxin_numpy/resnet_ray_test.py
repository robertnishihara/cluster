#!/usr/bin/env python
# File: resnet.b512.baseline.py
# Run for a few iterations and make sure accuracies are correct


import sys
import argparse
import numpy as np
import os
from itertools import count
import time

import tensorflow as tf

from tensorpack import *
from tensorpack.models import *
from tensorpack.callbacks import *
from tensorpack.train import TrainConfig, SyncMultiGPUTrainerParameterServer
from tensorpack.dataflow import imgaug
from tensorpack.tfutils import argscope, get_model_loader
from tensorpack.tfutils.summary import *
from tensorpack.utils.gpu import get_nr_gpu

from imagenet_utils import (
  ImageNetModel,
  get_imagenet_dataflow,
  fbresnet_augmentor_fast,
)

from resnet_model import (
  resnet_group, resnet_basicblock, resnet_bottleneck)


DATASET_SIZE=1281  # 0.1% of original dataset size
PER_GPU_BATCH_SIZE = 64
BASE_LR = 0.1 * (512 // 256)

class Model(ImageNetModel):
  def _get_optimizer(self):
    lr = tf.get_variable('learning_rate', initializer=0.1, trainable=False)
    tf.summary.scalar('learning_rate-summary', lr)
    return tf.train.GradientDescentOptimizer(lr)

  def get_logits(self, image):
    group_func = resnet_group
    block_func = resnet_bottleneck
    num_blocks = [3, 4, 6, 3]
    with argscope(
        [Conv2D, MaxPooling, GlobalAvgPooling, BatchNorm],
        data_format='NCHW'), \
        argscope(Conv2D, nl=tf.identity, use_bias=False,
                 W_init=tf.variance_scaling_initializer(scale=2.0, mode='fan_out')):
      logits = (LinearWrap(image)
                .Conv2D('conv0', 64, 7, stride=2, nl=BNReLU)
                .MaxPooling('pool0', shape=3, stride=2, padding='SAME')
                .apply(group_func, 'group0', block_func, 64, num_blocks[0], 1)
                .apply(group_func, 'group1', block_func, 128, num_blocks[1], 2)
                .apply(group_func, 'group2', block_func, 256, num_blocks[2], 2)
                .apply(group_func, 'group3', block_func, 512, num_blocks[3], 2)
                .GlobalAvgPooling('gap')
                .FullyConnected('linear', 1000, nl=tf.identity)())
      return logits

def get_data(name, batch):
  isTrain = name == 'train'
  global args
  augmentors = fbresnet_augmentor_fast(isTrain)

  if isTrain:
    print("Training batch:", batch)
    return get_imagenet_dataflow(args.data, name, batch, augmentors)
  else:
    imagenet1k = get_imagenet_dataflow(args.data, name, batch, augmentors)
    return imagenet1k

class StepTimeCallback(Callback):
  def _before_run(self, _):
    self._start = time.time()

  def _after_run(self, _, __):
    self.trainer.monitors.put_scalar('step-time', time.time() - self._start)

class EpochTimeCallback(Callback):
  def _before_epoch(self):
    self._start = time.time()

  def _after_epoch(self):
    self.trainer.monitors.put_scalar('epoch-time', time.time() - self._start)


class NumpyTrainer(SyncMultiGPUTrainerReplicated):

  var_values = None

  def _setup_graph(self, input, get_cost_fn, get_opt_fn):
    callbacks = super(NumpyTrainer, self)._setup_graph(input, get_cost_fn, get_opt_fn)
    self.all_vars = []  # #GPU x #PARAM
    for grads in self._builder.grads:
      self.all_vars.append([k[1] for k in grads])
    self.all_grads = [k[0] for k in self._builder.grads[0]]

    self.acc_values = None
    self.step_count = 0

    
    return callbacks

  def _get_values(self):
    self.var_values = self.sess.run(self.all_vars[0])

  def _set_values(self):
    for all_vars in self.all_vars:
      for val, var in zip(self.var_values, all_vars):
        var.load(val)

  def run_step(self):
    start_time = time.perf_counter()

    self.step_count+=1
    if self.var_values is None:
      self._get_values()

    grad_values = self.hooked_sess.run(self.all_grads)
    lr = 0.1
    momentum = 0.9

    for i in range(len(self.var_values)):
      v = self.var_values[i]
      g = grad_values[i]
      v -= lr*g
          
    self._set_values()
    
    duration = time.perf_counter() - start_time
    if self.step_count%1 == 0:  
      self.monitors.put_scalar('numpy-step-time', duration)


def get_config(model):
  nr_tower = max(get_nr_gpu(), 1)
  batch = PER_GPU_BATCH_SIZE

  logger.info("Running on {} towers. Batch size per tower: {}".format(nr_tower, batch))
  dataset_train = get_data('train', batch)
  dataset_val = get_data('val', batch)

  infs = [ClassificationError('wrong-top1', 'val-error-top1'),
          ClassificationError('wrong-top5', 'val-error-top5')]
  callbacks = [
    StepTimeCallback(),
    EpochTimeCallback(),
    GPUUtilizationTracker(),
  ]


  if args.fake:
    dataset_train = FakeData(
      [[batch, 224, 224, 3], [batch]], 1000,
      random=False, dtype=['uint8', 'int32'])


  input = QueueInput(dataset_train)
  input = StagingInput(input, nr_stage=1)

  num_gpus = get_nr_gpu()
  
  return TrainConfig(
    model=model,
    data=input,
    callbacks=callbacks,
    extra_callbacks=train.DEFAULT_CALLBACKS()+[
      MergeAllSummaries(period=1),
    ],
    steps_per_epoch=DATASET_SIZE // (PER_GPU_BATCH_SIZE*get_nr_gpu()),
    max_epoch=2,
  )


if __name__ == '__main__':
  from os.path import expanduser
  home = expanduser("~")

  parser = argparse.ArgumentParser()
  parser.add_argument('--gpu', default='0',
                      help='comma separated list of GPU(s) to use.')
  parser.add_argument('--data', default='/tmpfs/data/imagenet',
                      help='ILSVRC dataset dir')
  parser.add_argument('--load', help='load model')
  parser.add_argument('--fake', default=1, help='use fake data')
  parser.add_argument('--eval', action='store_true')
  parser.add_argument('--group', default='resnet_synthetic')
  parser.add_argument('--name', default='fresh00')
  args = parser.parse_args()

  if args.gpu:
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    os.environ['TF_CUDNN_USE_AUTOTUNE'] = '0'

  logdir = home+'/logs/resnet_synthetic_test'
  print("Writing to logging directory ", logdir)
  model = Model()
  logger.set_logger_dir(logdir, 'd')

  config = get_config(model)
  logger.set_logger_dir(logdir, 'd')

  nr_tower = max(get_nr_gpu(), 1)

  tf.set_random_seed(1)
  np.random.seed(1)
  #  launch_train_with_config(config, SyncMultiGPUTrainerReplicated(nr_tower))
  launch_train_with_config(config, NumpyTrainer(nr_tower,
                                                use_nccl=False))

  # get events from the events file
  step_times = []
  numpy_times = []
  epoch_times = []
  losses = []
  from tensorflow.python.summary import summary_iterator
  import glob

  for fname in glob.glob(logdir+'/events*'):
    print('opening ', fname)
    events = summary_iterator.summary_iterator(fname)

    events = [e for e in events if e.step]

    for event in events:
      step = event.step
      wall_time = event.wall_time
      vals = {val.tag: val.simple_value for val in event.summary.value}
      for tag in vals:
        if 'xentropy-loss' in tag:
          losses.append(vals[tag])
          #          print(step, tag, vals[tag])
        if tag == 'step-time':
          step_times.append(vals[tag])
        if tag == 'epoch-time':
          epoch_times.append(vals[tag])
        if tag == 'numpy-step-time':
          numpy_times.append(vals[tag])
          
  losses = np.array(losses)
  step_times = np.array(step_times)
  epoch_times = np.array(epoch_times)
  numpy_times = np.array(numpy_times)
  median_numpy_overhead_ms = 1000*np.median(numpy_times-step_times)
  print('Final loss: %10.5f' %(losses[-1]))
  print('Median step time: %10.1f ms'%( 1000*np.median(step_times)))
  print('Final epoch time: %10.3f sec' %(epoch_times[-1]))
  im_per_sec = DATASET_SIZE/epoch_times[-1]
  print('Images/second: %10.2f sec (%.1f on 8)' %(im_per_sec, 8*im_per_sec))
  print("Median numpy overhead: %.2f ms"%(median_numpy_overhead_ms))

  # Example two runs:
  #
  # Final loss:    0.04678
  # Median step time:      216.6 ms
  # Final epoch time:      7.210 sec
  # Images/second:     177.68 sec (1421.4 on 8)
  # Median numpy overhead: 142.06 ms

  # Final loss:    0.04678
  # Median step time:      218.2 ms
  # Final epoch time:      7.533 sec
  # Images/second:     170.06 sec (1360.5 on 8)
  # Median numpy overhead: 157.88 ms
  
  assert(abs(losses[-1]-0.046783510595560074)<0.001)
  assert(np.median(step_times)<0.23)
  assert(epoch_times[-1]<8)
  assert(epoch_times[-1]>4)
  assert(median_numpy_overhead_ms<170)
  print("Test passed")
