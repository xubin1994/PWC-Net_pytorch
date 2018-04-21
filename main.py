from datetime import datetime
import argparse
import imageio

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.utils.data import DataLoader

from model import Net
from losses import get_criterion
from dataset import (FlyingChairs, FlyingThings, Sintel, KITTI)

import tensorflow as tf

from logger import Logger
from pathlib import Path
from flow_utils import (flow_to_image, save_flow)


def parse():
    parser = argparse.ArgumentParser(description='Structure from Motion Learner training on KITTI and CityScapes Dataset',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    # mode selection
    # ============================================================
    parser.add_argument('--train', action = 'store_true')
    parser.add_argument('--predict', action = 'store_true')
    parser.add_argument('--test', action = 'store_true')



    # mode=train args
    # ============================================================
    
    parser.add_argument('--num_workers', default = 1, type = int, help = 'num of workers')
    parser.add_argument('--batch_size', default = 8, type=int, help='mini-batch size')
    parser.add_argument('--log_dir', default = 'train_log/' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--dataset_dir', type = str)
    parser.add_argument('--dataset', type = str)
    parser.add_argument('--weights', nargs = '+', type = float, default = [0.005, 0.01, 0.02, 0.08, 0.32, 1])
    parser.add_argument('--epsilon', default = 0.02)
    parser.add_argument('--q', default = 0.4)
    parser.add_argument('--gamma', default = 4e-4)
    parser.add_argument('--lr', default = 4e-4)
    parser.add_argument('--momentum', default = 4e-4)
    parser.add_argument('--beta', default = 0.99)
    parser.add_argument('--weight_decay', default = 4e-4)
    parser.add_argument('--total_step', default = 200 * 1000)
    # summary & log args
    parser.add_argument('--summary_interval', type = int, default = 100)
    parser.add_argument('--log_interval', type = int, default = 100)
    parser.add_argument('--checkpoint_interval', type = int, default = 100)



    # mode=predict args
    # ============================================================
    parser.add_argument('-i', '--input', nargs = 2)
    parser.add_argument('-o', '--output', default = 'output.flo')
    parser.add_argument('--load', type = str)



    # shared args
    # ============================================================
    parser.add_argument('--search_range', type = int, default = 4)
    parser.add_argument('--no_cuda', action = 'store_true')


    # image input size
    # ============================================================
    parser.add_argument('--crop_shape', type = int, nargs = '+', default = [384, 448])
    parser.add_argument('--resize_shape', nargs = 2, type = int, default = None)
    parser.add_argument('--resize_scale', type = float, default = None)
    parser.add_argument('--num_levels', type = int, default = 6)
    parser.add_argument('--lv_chs', nargs = '+', type = int, default = [16, 32, 64, 96, 128, 192])

    
    

    args = parser.parse_args()
    # check args
    # ============================================================
    if args.train:
        assert not(args.predict or args.test), 'Only ONE mode should be selected.'
        assert len(args.weights) == len(args.lv_chs) == args.num_levels
        assert args.dataset in ['FlyingChairs', 'FlyingThings', 'Sintel', 'KITTI'], 'One dataset should be correctly set as for there are specific hyper-parameters for every dataset'
    elif args.predict:
        assert not(args.train or args.test), 'Only ONE mode should be selected.'
        assert args.input is not None, 'TWO input image path should be given.'
        assert args.load is not None
    elif args.test:
        assert not(args.train or args.predict), 'Only ONE mode should be selected.'
        assert args.load is not None
    else:
        raise RuntimeError('use --train/predict/test to select a mode')

    return args


def train(args):
    # Build Model
    # ============================================================
    model = Net(args)
    if not args.no_cuda: model.cuda_()

    # TODO: change optimizer to S_long & S_fine (same as flownet2)
    
    # build criterion
    criterion = get_criterion(args)
    optimizer = torch.optim.Adam(model.parameters(), args.lr,
                                 betas = (args.momentum, args.beta),
                                 weight_decay = args.weight_decay)


    
    # Prepare Dataloader
    # ============================================================
    train_dataset, eval_dataset = eval("{0}('{1}', 'train', crop_shape = {2}, resize_shape = {3}, resize_scale = {4}), {0}('{1}', 'test', crop_shape = {2}, resize_shape = {3}, resize_scale = {4})".format(args.dataset, args.dataset_dir, args.crop_shape, args.resize_shape, args.resize_scale))

    train_loader = DataLoader(train_dataset,
                            batch_size = args.batch_size,
                            shuffle = True,
                            num_workers = args.num_workers,
                            pin_memory = True)
    eval_loader = DataLoader(eval_dataset,
                            batch_size = args.batch_size,
                            shuffle = True,
                            num_workers = args.num_workers,
                            pin_memory = True)



    # Init logger
    logger = Logger(args.log_dir)
    p_log = Path(args.log_dir)

    # Start training
    # ============================================================
    data_iter = iter(train_loader)
    iter_per_epoch = len(train_loader)
    model.train()
    for step in range(1, args.total_step + 1):
        # Reset the data_iter
        if (step) % iter_per_epoch == 0: data_iter = iter(train_loader)

        # Load Data
        # ============================================================
        data, target = next(data_iter)
        # shape: B,3,H,W
        src_img, tgt_img = map(torch.squeeze, data[0].split(split_size = 1, dim = 2))
        # shape: B,2,H,W
        flow_gt = target[0]
        if not args.no_cuda: src_img, tgt_img, flow_gt = map(lambda x: x.cuda(), (src_img, tgt_img, flow_gt))
        src_img, tgt_img, flow_gt = map(Variable, (src_img, tgt_img, flow_gt))


        
        # Build Groundtruth Pyramid
        # ============================================================
        flow_gt_pyramid = []
        x = flow_gt
        for l in range(args.num_levels):
            x = F.avg_pool2d(x, 2)
            flow_gt_pyramid.insert(0, x)


        
        # Forward Pass
        # ============================================================
        # features on each level will downsample to 1/2 from bottom to top
        flow_pyramid, summaries = model(src_img, tgt_img)

        
        # Compute Loss
        # ============================================================
        loss = criterion(args, flow_pyramid, flow_gt_pyramid)
        epe = 0


        
        # Do step
        # ============================================================
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()


        
        # Collect Summaries & Output Logs
        # ============================================================
        # TODO: add summaries and check
        # flow output on each level
        if True:
        # if step % args.summary_interval == 0:
            # add scalar summaries
            logger.scalar_summary('loss', loss.data[0], step)
            logger.scalar_summary('EPE', epe, step)


            # add image summaries
            for l in range(args.num_levels): ...
                # logger.image_summary(f'flow_level{l}', [flow_pyramid[l]], step)
                # logger.image_summary(f'input_1', [src_img], step)
                # logger.image_summary(f'')
        # save model
        if step % args.checkpoint_interval == 0:
            torch.save(model.state_dict(), str(p_log / f'{step}.pkl'))
        # print log
        if step % args.log_interval == 0:
            print(f'Step [{step}/{args.total_step}], Loss: {loss.data[0]:.4f}, EPE: {epe:.4f}')


def predict(args):
    # TODO
    # Build Model
    # ============================================================
    model = Net(args)
    if not args.no_cuda: model.cuda_()
    model.load_state_dict(torch.load(args.load))
    model.eval()
    
    # Load Data
    # ============================================================
    src_img, tgt_img = map(imageio.imread, args.input)
    src_img = np.array(src_img)[np.newaxis,:,:,:].transpose(0,3,1,2)
    tgt_img = np.array(tgt_img)[np.newaxis,:,:,:].transpose(0,3,1,2)

    class StaticCenterCrop(object):
        def __init__(self, image_size, crop_size):
            self.th, self.tw = crop_size
            self.h, self.w = image_size
        def __call__(self, img):
            return img[(self.h-self.th)/2:(self.h+self.th)/2, (self.w-self.tw)/2:(self.w+self.tw)/2,:]


    if args.crop_shape is not None:
        cropper = StaticCenterCrop(src_img.shape[:2], args.crop_shape)
        src_img, tgt_img = map(cropper, [src_img, tgt_img])
    if args.resize_shape is not None:
        resizer = partial(cv2.resize, dsize = (0,0), dst = args.resize_shape)
        src_img, tgt_img = map(resizer, [src_img, tgt_img])
    elif args.resize_scale is not None:
        resizer = partial(cv2.resize, dsize = (0,0), fx = args.resize_scale, fy = args.resize_scale)
        src_img, tgt_img = map(resizer, [src_img, tgt_img])


    src_img = Variable(torch.Tensor(src_img))
    tgt_img = Variable(torch.Tensor(tgt_img))
    if not args.no_cuda: src_img, tgt_img = map(lambda x: x.cuda(), [src_img, tgt_img])
    

    # Forward Pass
    # ============================================================
    flow_pyramid = model(src_img, tgt_img)
    flow = flow_pyramid[-1]
    save_flow(args.output, flow)
    flow_vis = flow_to_image(flow)
    imageio.imwrite(args.output.replace('.flo', '.png'), flow_vis)
    import matplotlib.pyplot as plt
    plt.imshow(flow_vis)
    plt.show()



def test(args):
    # TODO
    pass


def main(args):
    if args.train: train(args)
    elif args.predict: predict(args)
    else: test(args)


if __name__ == '__main__':
    args = parse()
    main(args)