from lib import environ, models, validation
from common import agent, experience, helper
from datetime import datetime, date
import os
import time
import logging
import argparse
import collections
import gym
import numpy as np
import pandas as pd
import torch
import torch.optim as optim

from common.writer import SummaryWriter


BATCH_SIZE = 32
BARS_COUNT = 100
REWARD_GROUPS = 100

REPLAY_SIZE = 100000
REPLAY_INITIAL = 10000
REWARD_STEPS = 2
LEARNING_RATE = 0.0001
TARGET_NET_SYNC = 1000

STATES_TO_EVALUATE = 1000
EVAL_EVERY_STEP = 1000
VALIDATION_EVERY_STEP = 100000
CHECKPOINT_EVERY_STEP = 50000
GOOGLE_COLAB_MAX_STEP = 500000

EPSILON_START = 1.0
EPSILON_FINAL = 0.1
EPSILON_STEPS = 1000000


def calc_qvals(rewards):
    sum = 0
    buf = []
    for reward in reversed(rewards):
        sum *= GAMMA
        sum += reward
        buf.append(sum)
    return list(reversed(buf))


parser = argparse.ArgumentParser()
parser.add_argument('--resume', default='', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
parser.add_argument('--cuda', default=False, action='store_true', help='enable cuda')
parser.add_argument('--colab', default=False, action='store_true', help='enable colab hosted runtime')
parser.add_argument('--double', default=False, action='store_true', help='enable double DQN')
parser.add_argument('-n', '--name', required=True, type=str, help='run name')
args = parser.parse_args()

device = torch.device('cuda' if args.cuda else 'cpu')

try:
    from lib import data
    train_data = data.read_csv(file_name='data/000001_2017.csv')
    val_data = data.read_csv(file_name='data/000001_2018.csv')
except ModuleNotFoundError:
    train_data = (pd.read_csv('data/prices_2017.csv', index_col=0),
                  pd.read_csv('data/factors_2017.csv', index_col=0))
    val_data = (pd.read_csv('data/prices_2018.csv', index_col=0),
                pd.read_csv('data/factors_2018.csv', index_col=0))

env = environ.StockEnv(train_data, bars_count=BARS_COUNT, reset_on_sell=True)
env = gym.wrappers.TimeLimit(env, max_episode_steps=1000)
env_test = environ.StockEnv(train_data, bars_count=BARS_COUNT, reset_on_sell=True)
env_test = gym.wrappers.TimeLimit(env_test, max_episode_steps=1000)
env_val = environ.StockEnv(val_data, bars_count=BARS_COUNT, reset_on_sell=True)
env_val = gym.wrappers.TimeLimit(env_val, max_episode_steps=1000)

net = models.DQNConv1d(env.observation_space.shape, env.action_space.n).to(device)
tgt_net = models.DQNConv1d(env.observation_space.shape, env.action_space.n).to(device)

agent = agent.EpsilonGreedyAgent(net, env, epsilon=EPSILON_START, device=device)
exp_source = experience.ExperienceSource(env, agent, GAMMA)
buffer = experience.ExperienceBuffer(exp_source, REPLAY_SIZE)
optimizer = optim.Adam(net.parameters(), lr=LEARNING_RATE)

total_reward = []
total_steps = []
reward_buf = []
steps_buf = []
batch_states, batch_actions, batch_rewards, batch_qvals = [], [], [], []
frame_idx = 0
frame_prev = 0
done_episodes = 0
batch_episodes = 0
ts = time.time()

eval_states = None
best_mean_val = None

datestr = datetime.strftime(date(2019, 2, 2), '%Y-%m-%d')
save_path = os.path.join('saves', datestr + '_' + args.name)
os.makedirs(save_path, exist_ok=True)

if args.resume:
    print('Loading %s' % args.resume)
    checkpoint = torch.load(os.path.join(save_path, 'checkpoints', args.resume))
    total_reward = checkpoint['total_reward']
    total_steps = checkpoint['total_steps']
    frame_idx = checkpoint['frame_idx']
    eval_states = checkpoint['eval_states']
    best_mean_val = checkpoint['best_mean_val']
    net.load_state_dict(checkpoint['state_dict']),
    tgt_net.load_state_dict(checkpoint['state_dict']),
    optimizer.load_state_dict(checkpoint['optimizer'])
    print('Loaded %s' % args.resume)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(message)s',
                    handlers=[logging.FileHandler(os.path.join(save_path, 'console.log')),
                              logging.StreamHandler()])

writer = SummaryWriter(os.path.join('runs', datestr + '_' + args.name))


for exp in iter(exp_source):
    frame_idx += 1
    batch_states.append(exp.state)
    batch_actions.append(exp.actions)
    batch_rewards.append(exp.reward)
    if exp.last_state is None:
        batch_qvals.extend(calc_qvals(batch_rewards))
        batch_rewards.clear()
        batch_episodes += 1

    if batch_episodes < EPISODES_TO_TRAIN:
        continue

    optimizer.zero_grad()
    states_v = torch.FloatTensor(batch_states).to(device)
    actions_v = torch.LongTensor(batch_actions).to(device)
    qvals_v = torch.FloatTensor(batch_qvals).to(device)

    logits_v = net(states_v)
    log_probs_v = F.log_softmax(logits_v, dim=1)
    policy_gradient = qvals_v * log_probs_v[range(len(states_v)), actions_v]
    loss = - policy_gradient.mean()
    loss.backward()
    optimizer.step()

    batch_episodes = 0
    batch_states.clear()
    batch_actions.clear()
    batch_qvals.clear()

    ep_reward, ep_steps = exp_source.pop_episode_result()
    if ep_reward:
        reward_buf.append(ep_reward)
        steps_buf.append(ep_steps)
        if len(reward_buf) == REWARD_GROUPS:
            reward = np.mean(reward_buf)
            steps = np.mean(steps_buf)
            reward_buf.clear()
            steps_buf.clear()
            total_reward.append(reward)
            total_steps.append(steps)
            speed = (frame_idx - frame_prev) / (time.time() - ts)
            frame_prev = frame_idx
            ts = time.time()
            mean_reward = np.mean(total_reward[-100:])
            mean_step = np.mean(total_steps[-100:])
            logger.info('%d done %d games, mean reward %.3f, mean step %d, epsilon %.2f, speed %.2f f/s' % (frame_idx, len(total_reward), mean_reward, mean_step, agent.epsilon, speed))
            writer.add_scalar('epsilon', agent.epsilon, frame_idx)
            writer.add_scalar('speed', speed, frame_idx)
            writer.add_scalar('reward', reward, frame_idx)
            writer.add_scalar('reward_100', mean_reward, frame_idx)
            writer.add_scalar('steps', steps, frame_idx)
            writer.add_scalar('steps_100', mean_step, frame_idx)

    if eval_states is None:
        print('Initial buffer populated, start training')
        eval_states = buffer.sample(STATES_TO_EVALUATE)
        eval_states = np.array([np.array(exp.state, copy=False)
                                for exp in eval_states], copy=False)

    if frame_idx % EVAL_EVERY_STEP == 0:
        mean_vals = []
        for batch in np.array_split(eval_states, 64):
            states_v = torch.tensor(batch).to(device)
            action_values_v = net(states_v)
            best_action_values_v = action_values_v.max(1)[0]
            mean_vals.append(best_action_values_v.mean().item())
        mean_val = np.mean(mean_vals)
        writer.add_scalar('values_mean', mean_val, frame_idx)
        if best_mean_val is None or best_mean_val < mean_val:
            torch.save(net.state_dict(), os.path.join(save_path, 'best_mean_val.pth'))
            if best_mean_val is not None:
                logger.info('Best mean value updated %.3f -> %.3f'
                            % (best_mean_val, mean_val))
            best_mean_val = mean_val

    if frame_idx % TARGET_NET_SYNC == 0:
        tgt_net.load_state_dict(net.state_dict())

    if frame_idx % VALIDATION_EVERY_STEP == 0:
        res = validation.run_val(env_test, net, device=device)
        logger.info('%d test done, reward %.3f, step %d' % (frame_idx, res['episode_rewards'], res['episode_steps']))
        for key, val in res.items():
            writer.add_scalar(key + '_test', val, frame_idx)
        res = validation.run_val(env_val, net, device=device)
        logger.info('%d validation done, reward %.3f, step %d' % (frame_idx, res['episode_rewards'], res['episode_steps']))
        for key, val in res.items():
            writer.add_scalar(key + '_val', val, frame_idx)

    if frame_idx % CHECKPOINT_EVERY_STEP == 0:
        checkpoint = {'frame_idx': frame_idx,
                      'state_dict': net.state_dict(),
                      'optimizer': optimizer.state_dict(),
                      'total_reward': total_reward,
                      'total_steps': total_steps,
                      'eval_states': eval_states,
                      'best_mean_val': best_mean_val}
        torch.save(checkpoint, os.path.join(save_path, 'checkpoints', 'checkpoint-%d.pth' % frame_idx))
        print('checkpoint saved at frame %d' % frame_idx)

    # workaround Colab's time limit
    if args.colab:
        if frame_idx % GOOGLE_COLAB_MAX_STEP == 0:
            break

writer.close()