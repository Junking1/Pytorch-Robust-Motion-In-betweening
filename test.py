import torch
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from LaFan import LaFan1
from torch.utils.data import Dataset, DataLoader
from model import StateEncoder, \
                  OffsetEncoder, \
                  TargetEncoder, \
                  LSTM, \
                  Decoder, \
                  ShortMotionDiscriminator, \
                  LongMotionDiscriminator
from skeleton import Skeleton
import torch.optim as optim
from tensorboardX import SummaryWriter
import numpy as np
from tqdm import tqdm
from functions import gen_ztta, write_to_bvhfile
import yaml
import time
import shutil
import imageio
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import axes3d, Axes3D
# from remove_fs import remove_fs

def plot_pose(pose, cur_frame, prefix):
    
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')

    ax.cla()
    num_joint = pose.shape[0] // 3
    ax.scatter(pose[:num_joint, 0], pose[:num_joint, 2], pose[:num_joint, 1], c='r')
    ax.scatter(pose[num_joint:num_joint*2, 0], pose[num_joint:num_joint*2, 2], pose[num_joint:num_joint*2, 1],c='b')
    ax.scatter(pose[num_joint*2:num_joint*3, 0], pose[num_joint*2:num_joint*3, 2], pose[num_joint*2:num_joint*3, 1],c='g')
    xmin = np.min(pose[:, 0])
    ymin = np.min(pose[:, 2])
    zmin = np.min(pose[:, 1])
    xmax = np.max(pose[:, 0])
    ymax = np.max(pose[:, 2])
    zmax = np.max(pose[:, 1])
    scale = np.max([xmax - xmin, ymax - ymin, zmax - zmin])
    xmid = (xmax + xmin) // 2
    ymid = (ymax + ymin) // 2
    zmid = (zmax + zmin) // 2
    ax.set_xlim(xmid - scale // 2, xmid + scale // 2)
    ax.set_ylim(ymid - scale // 2, ymid + scale // 2)
    ax.set_zlim(zmid - scale // 2, zmid + scale // 2)

    plt.draw()
    plt.savefig(prefix + '_' + str(cur_frame)+'.png', dpi=200, bbox_inches='tight')
    plt.close()

if __name__ == '__main__':
    opt = yaml.load(open('./config/test-base.yaml', 'r').read())
    model_dir =opt['test']['model_dir']

    
    ## initilize the skeleton ##
    skeleton_mocap = Skeleton(offsets=opt['data']['offsets'], parents=opt['data']['parents'])
    skeleton_mocap.cuda()
    skeleton_mocap.remove_joints(opt['data']['joints_to_remove'])

    ## load train data ##
    lafan_data_test = LaFan1(opt['data']['data_dir'], \
                              seq_len = opt['model']['seq_length'], \
                              train = True, debug=opt['test']['debug'])
    x_mean = lafan_data_test.x_mean.cuda()
    x_std = lafan_data_test.x_std.cuda().view(1, 1, opt['model']['num_joints'], 3)
    lafan_loader_train = DataLoader(lafan_data_test, \
                                    batch_size=opt['test']['batch_size'], \
                                    shuffle=True, num_workers=opt['data']['num_workers'])

    ## initialize model and load parameters ##
    state_encoder = StateEncoder(in_dim=opt['model']['state_input_dim'])
    state_encoder = state_encoder.cuda()
    state_encoder.load_state_dict(torch.load(os.path.join(opt['test']['model_dir'], 'state_encoder.pkl')))
    offset_encoder = OffsetEncoder(in_dim=opt['model']['offset_input_dim'])
    offset_encoder = offset_encoder.cuda()
    offset_encoder.load_state_dict(torch.load(os.path.join(opt['test']['model_dir'], 'offset_encoder.pkl')))
    target_encoder = TargetEncoder(in_dim=opt['model']['target_input_dim'])
    target_encoder = target_encoder.cuda()
    target_encoder.load_state_dict(torch.load(os.path.join(opt['test']['model_dir'], 'target_encoder.pkl')))
    lstm = LSTM(in_dim=opt['model']['lstm_dim'], hidden_dim = opt['model']['lstm_dim'] * 2)
    lstm = lstm.cuda()
    lstm.load_state_dict(torch.load(os.path.join(opt['test']['model_dir'], 'lstm.pkl')))
    decoder = Decoder(in_dim=opt['model']['lstm_dim'] * 2, out_dim=opt['model']['state_input_dim'])
    decoder = decoder.cuda()
    decoder.load_state_dict(torch.load(os.path.join(opt['test']['model_dir'], 'decoder.pkl')))
    print('model loaded')

    ## get positional code ##
    if opt['test']['use_ztta']:
        ztta = gen_ztta().cuda()
    # print('ztta:', ztta.size())
    # assert 0
    
    # writer = SummaryWriter(log_dir)
    loss_total_min = 10000000.0
    for epoch in range(opt['test']['num_epoch']):
        state_encoder.eval()
        offset_encoder.eval()
        target_encoder.eval()
        lstm.eval()
        decoder.eval()
        loss_total_list = []
        
        for i_batch, sampled_batch in enumerate(lafan_loader_train):
            pred_img_list = []
            gt_img_list = []
            img_list = []

            # print(i_batch, sample_batched['local_q'].size())
            
            loss_pos = 0
            loss_quat = 0
            loss_contact = 0
            loss_root = 0
            with torch.no_grad():
                # if True:
                # state input
                local_q = sampled_batch['local_q'].cuda()
                root_v = sampled_batch['root_v'].cuda()
                contact = sampled_batch['contact'].cuda()
                # offset input
                root_p_offset = sampled_batch['root_p_offset'].cuda()
                local_q_offset = sampled_batch['local_q_offset'].cuda()
                local_q_offset = local_q_offset.view(local_q_offset.size(0), -1)
                # target input
                target = sampled_batch['target'].cuda()
                target = target.view(target.size(0), -1)
                # root pos
                root_p = sampled_batch['root_p'].cuda()
                # X
                X = sampled_batch['X'].cuda()
                bs = np.random.choice(X.size(0), 1)[0]
                if False:
                    print('local_q:', local_q.size(), \
                        'root_v:', root_v.size(), \
                        'contact:', contact.size(), \
                        'root_p_offset:', root_p_offset.size(), \
                        'local_q_offset:', local_q_offset.size(), \
                        'target:', target.size())
                
                lstm.init_hidden(local_q.size(0))
                h_list = []
                pred_list = []
                pred_list.append(X[:,0])
                bvh_list = []
                bvh_list.append(torch.cat([X[:,0,0], local_q[:,0,].view(local_q.size(0), -1)], -1))
                # print(X.size())
                for t in range(opt['model']['seq_length'] - 1):
                    # root pos
                    if t  == 0:
                        root_p_t = root_p[:,t]
                        local_q_t = local_q[:,t]
                        local_q_t = local_q_t.view(local_q_t.size(0), -1)
                        contact_t = contact[:,t]
                        root_v_t = root_v[:,t]
                    else:
                        root_p_t = root_pred[0]
                        local_q_t = local_q_pred[0]
                        contact_t = contact_pred[0]
                        root_v_t = root_v_pred[0]
                        
                    # state input
                    state_input = torch.cat([local_q_t, root_v_t, contact_t], -1)
                    # offset input
                    root_p_offset_t = root_p_offset - root_p_t
                    local_q_offset_t = local_q_offset - local_q_t
                    # print('root_p_offset_t:', root_p_offset_t.size(), 'local_q_offset_t:', local_q_offset_t.size())
                    offset_input = torch.cat([root_p_offset_t, local_q_offset_t], -1)
                    # target input
                    target_input = target
                    

                    # print('state_input:',state_input.size())
                    h_state = state_encoder(state_input)
                    h_offset = offset_encoder(offset_input)
                    h_target = target_encoder(target_input)
                    
                    if opt['test']['use_ztta']:
                        h_state += ztta[:, t]
                        h_offset += ztta[:, t]
                        h_target += ztta[:, t]

                    if opt['test']['use_adv']:
                        if t < 5:
                            lambda_target = 0.0
                        elif t >=5 and t < 30:
                            lambda_target = (t - 5) / 25.0
                        else:
                            lambda_target = 1.0
                        h_offset += 0.5 * lambda_target * torch.cuda.FloatTensor(h_offset.size()).normal_()
                        h_target += 0.5 * lambda_target * torch.cuda.FloatTensor(h_target.size()).normal_()

                    h_in = torch.cat([h_state, h_offset, h_target], -1).unsqueeze(0)
                    h_out = lstm(h_in)
                    # print('h_out:', h_out.size())
                
                    h_pred, contact_pred = decoder(h_out)
                    local_q_v_pred = h_pred[:,:,:opt['model']['target_input_dim']]
                    local_q_pred = local_q_v_pred + local_q_t
                    # print('q_pred:', q_pred.size())
                    local_q_pred_ = local_q_pred.view(local_q_pred.size(0), local_q_pred.size(1), -1, 4)
                    local_q_pred_ = local_q_pred_ / torch.norm(local_q_pred_, dim = -1, keepdim = True)
                    # print("local_q_pred_:", local_q_pred_.size())
                    root_v_pred = h_pred[:,:,opt['model']['target_input_dim']:]
                    root_pred = root_v_pred + root_p_t
                    # print(''contact:'', contact_pred.size())
                    # print('root_pred:', root_pred.size())
                    bvh_list.append(torch.cat([root_pred[0], local_q_pred_[0].view(local_q_pred_.size(1), -1)], -1))
                    pos_pred = skeleton_mocap.forward_kinematics(local_q_pred_, root_pred)

                    pos_next = X[:,t+1]
                    local_q_next = local_q[:,t+1]
                    local_q_next = local_q_next.view(local_q_next.size(0), -1)
                    root_p_next = root_p[:,t+1]
                    contact_next = contact[:,t+1]
                    # print(pos_pred.size(), x_std.size())
                    loss_pos += torch.mean(torch.abs(pos_pred[0] - pos_next) / x_std) / opt['model']['seq_length']
                    loss_quat += torch.mean(torch.abs(local_q_pred[0] - local_q_next)) / opt['model']['seq_length']
                    loss_root += torch.mean(torch.abs(root_pred[0] - root_p_next) / x_std[:,:,0]) / opt['model']['seq_length']
                    loss_contact += torch.mean(torch.abs(contact_pred[0] - contact_next)) / opt['model']['seq_length']
                    pred_list.append(pos_pred[0])

                    if i_batch < 49:
                        # print("pos_pred:", pos_pred.size())
                        if opt['test']['save_img']:
                            plot_pose(np.concatenate([X[bs,0].view(22, 3).detach().cpu().numpy(),\
                                                 pos_pred[0, bs].view(22, 3).detach().cpu().numpy(),\
                                                 X[bs,-1].view(22, 3).detach().cpu().numpy()], 0),\
                                                 t, '../results/pred')
                            plot_pose(np.concatenate([X[bs,0].view(22, 3).detach().cpu().numpy(),\
                                                 X[bs,t+1].view(22, 3).detach().cpu().numpy(),\
                                                 X[bs,-1].view(22, 3).detach().cpu().numpy()], 0),\
                                                 t, '../results/gt')
                            pred_img = imageio.imread('../results/pred_'+str(t)+'.png')
                            gt_img = imageio.imread('../results/gt_'+str(t)+'.png')
                            pred_img_list.append(pred_img)
                            gt_img_list.append(gt_img)
                            img_list.append(np.concatenate([pred_img, gt_img], 1))
                if opt['test']['save_bvh']:
                    # print("bs:", bs)
                    bvh_data = torch.cat([x[bs].unsqueeze(0) for x in bvh_list], 0).detach().cpu().numpy()
                    # print('bvh_data:', bvh_data.shape)
                    write_to_bvhfile(bvh_data, ('../bvh_seq/test_%03d.bvh' % i_batch), opt['data']['joints_to_remove'])
                    # assert 0

                if i_batch < 49:
                    if opt['test']['save_img'] and opt['test']['save_gif']:
                        imageio.mimsave(('../gif/img_%03d.gif' % i_batch), img_list, duration=0.1)
                if opt['test']['save_pose']:
                    gt_pose = X[bs,:].view(opt['model']['seq_length'], 22, 3).detach().cpu().numpy()
                    pred_pose = torch.cat([x[bs].unsqueeze(0) for x in pred_list], 0).detach().cpu().numpy()
                    plt.clf()
                    joint_idx = 13
                    plt.plot(range(opt['model']['seq_length']), gt_pose[:,joint_idx,0])
                    plt.plot(range(opt['model']['seq_length']), pred_pose[:,joint_idx,0])
                    plt.legend(['gt', 'pred'])
                    plt.savefig('../results/pose_%03d.png' % i_batch)
                    plt.close()

                if opt['test']['save_img'] and i_batch > 49:
                    break

                if opt['test']['save_pose'] and i_batch > 49:
                    break
                
        # print("train epoch: %03d, cur total loss:%.3f, cur best loss:%.3f" % (epoch, loss_total_cur, loss_total_min))
