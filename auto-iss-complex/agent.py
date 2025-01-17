import os
from collections import deque, namedtuple

from load_env import enviroment
import time
import csv

import numpy as np
import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader
from torch.optim import Adam
from torch.distributions import Categorical
import torch.nn.functional as F

import gym

# constants

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# data

Memory = namedtuple('Memory', ['state', 'action', 'action_log_prob', 'reward', 'done', 'value'])
AuxMemory = namedtuple('Memory', ['state', 'target_value', 'old_values'])

class ExperienceDataset(Dataset):
    def __init__(self, data):
        super().__init__()
        self.data = data

    def __len__(self):
        return len(self.data[0])

    def __getitem__(self, ind):
        return tuple(map(lambda t: t[ind], self.data))

def create_shuffled_dataloader(data, batch_size):
    ds = ExperienceDataset(data)
    return DataLoader(ds, batch_size = batch_size, shuffle = True)

# helpers

def exists(val):
    return val is not None

def normalize(t, eps = 1e-5):
    return (t - t.mean()) / (t.std() + eps)

def update_network_(loss, optimizer):
    optimizer.zero_grad()
    loss.mean().backward()
    optimizer.step()

def init_(m):
    if isinstance(m, nn.Linear):
        gain = torch.nn.init.calculate_gain('tanh')
        torch.nn.init.orthogonal_(m.weight, gain)
        if m.bias is not None:
            torch.nn.init.zeros_(m.bias)

# networks

class Actor(nn.Module):
    def __init__(self, state_dim, hidden_dim, num_actions):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh()
        )

        self.action_head = nn.Sequential(
            nn.Linear(hidden_dim, num_actions),
            nn.Softmax(dim=-1)
        )

        self.value_head = nn.Linear(hidden_dim, 1)
        self.apply(init_)

    def forward(self, x):
        hidden = self.net(x)
        return self.action_head(hidden), self.value_head(hidden)

class Critic(nn.Module):
    def __init__(self, state_dim, hidden_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, 1),
        )
        self.apply(init_)

    def forward(self, x):
        return self.net(x)

# agent

def clipped_value_loss(values, rewards, old_values, clip):
    value_clipped = old_values + (values - old_values).clamp(-clip, clip)
    value_loss_1 = (value_clipped.flatten() - rewards) ** 2
    value_loss_2 = (values.flatten() - rewards) ** 2
    return torch.mean(torch.max(value_loss_1, value_loss_2))

class PPG:
    def __init__(
        self,
        state_dim,
        num_actions,
        actor_hidden_dim,
        critic_hidden_dim,
        epochs,
        epochs_aux,
        minibatch_size,
        lr,
        lam,
        gamma,
        beta_s,
        eps_clip,
        value_clip,
        save_path
    ):
        self.actor = Actor(state_dim, actor_hidden_dim, num_actions).to(device)
        self.critic = Critic(state_dim, critic_hidden_dim).to(device)
        self.opt_actor = Adam(self.actor.parameters(), lr=lr)
        self.opt_critic = Adam(self.critic.parameters(), lr=lr)

        self.minibatch_size = minibatch_size

        self.epochs = epochs
        self.epochs_aux = epochs_aux

        self.lam = lam
        self.gamma = gamma
        self.beta_s = beta_s

        self.eps_clip = eps_clip
        self.value_clip = value_clip
        self.save_path = save_path

    def save(self):
        torch.save({
            'actor': self.actor.state_dict(),
            'critic': self.critic.state_dict()
        }, f'./'+ self.save_path + '.pt')

    def load(self,path):
        if not os.path.exists(path):
            print('El archivo seleccionado: '+path+' no existe')
            return
        
        print('Agente cardado correctamente')
        data = torch.load(f'./'+ path)
        self.actor.load_state_dict(data['actor'])
        self.critic.load_state_dict(data['critic'])
        
        
    def display_info(self,steps,max_steps,mean_reward,mean_time):
        print('Steps = ' + str(steps+1) +'/'+ str(max_steps) +' | Mean Reward = ' + str(np.mean(mean_reward, dtype=np.float16)) + ' | Mean Iteration time = ' +str(np.mean(mean_time, dtype=np.float16))+' s', end='\r')
        if steps == max_steps-1:
            print('Steps = ' + str(steps+1) +'/'+ str(max_steps) + ' | Mean Reward = ' + str(np.mean(mean_reward, dtype=np.float16)) + 
                  ' | Mean Iteration time = ' +str(np.mean(mean_time, dtype=np.float16))+ ' s | Total Episode Time = ' + str(np.sum(mean_time, dtype=np.float16))+'s')   
            with open(self.save_path + '.csv','a+',newline = '') as file:
                writer = csv.writer(file)
                writer.writerow([np.mean(mean_reward, dtype=np.float16)])
                file.close() 
        return 
        

    def learn(self, memories, aux_memories, next_state):
        # retrieve and prepare data from memory for training
        states = []
        actions = []
        old_log_probs = []
        rewards = []
        masks = []
        values = []

        for mem in memories:
            states.append(torch.tensor(mem.state))
            actions.append(torch.tensor(mem.action))
            old_log_probs.append(mem.action_log_prob)
            rewards.append(mem.reward)
            masks.append(1 - float(mem.done))
            values.append(mem.value)

        # calculate generalized advantage estimate
        next_state = torch.from_numpy(np.asarray(next_state)).float()
        next_value = self.critic(next_state).detach()
        values = values + [next_value]

        returns = []
        gae = 0
        for i in reversed(range(len(rewards))):
            delta = rewards[i] + self.gamma * values[i + 1] * masks[i] - values[i]
            gae = delta + self.gamma * self.lam * masks[i] * gae
            returns.insert(0, gae + values[i])

        # convert values to torch tensors
        to_torch_tensor = lambda t: torch.stack(t).to(device).detach()

        states = to_torch_tensor(states)
        actions = to_torch_tensor(actions)
        old_values = to_torch_tensor(values[:-1])
        old_log_probs = to_torch_tensor(old_log_probs)

        rewards = torch.tensor(returns).to(device)

        # store state and target values to auxiliary memory buffer for later training
        aux_memory = AuxMemory(states, rewards, old_values)
        aux_memories.append(aux_memory)

        # prepare dataloader for policy phase training
        dl = create_shuffled_dataloader([states, actions, old_log_probs, rewards, old_values], self.minibatch_size)

        # policy phase training, similar to original PPO
        for _ in range(self.epochs):
            for states, actions, old_log_probs, rewards, old_values in dl:
                action_probs, _ = self.actor(states)
                values = self.critic(states)
                dist = Categorical(action_probs)
                action_log_probs = dist.log_prob(actions)
                entropy = dist.entropy()

                # calculate clipped surrogate objective, classic PPO loss
                ratios = (action_log_probs - old_log_probs).exp()
                advantages = normalize(rewards - old_values.detach())
                surr1 = ratios * advantages
                surr2 = ratios.clamp(1 - self.eps_clip, 1 + self.eps_clip) * advantages
                policy_loss = - torch.min(surr1, surr2) - self.beta_s * entropy

                update_network_(policy_loss, self.opt_actor)

                # calculate value loss and update value network separate from policy network
                value_loss = clipped_value_loss(values, rewards, old_values, self.value_clip)

                update_network_(value_loss, self.opt_critic)

    def learn_aux(self, aux_memories):
        # gather states and target values into one tensor
        states = []
        rewards = []
        old_values = []
        for state, reward, old_value in aux_memories:
            states.append(state)
            rewards.append(reward)
            old_values.append(old_value)

        states = torch.cat(states)
        rewards = torch.cat(rewards)
        old_values = torch.cat(old_values)

        # get old action predictions for minimizing kl divergence and clipping respectively
        old_action_probs, _ = self.actor(states)
        old_action_probs.detach_()

        # prepared dataloader for auxiliary phase training
        dl = create_shuffled_dataloader([states, old_action_probs, rewards, old_values], self.minibatch_size)

        # the proposed auxiliary phase training
        # where the value is distilled into the policy network, while making sure the policy network does not change the action predictions (kl div loss)
        for epoch in range(self.epochs_aux):
            for states, old_action_probs, rewards, old_values in dl:
                action_probs, policy_values = self.actor(states)
                action_logprobs = action_probs.log()

                # policy network loss copmoses of both the kl div loss as well as the auxiliary loss
                aux_loss = clipped_value_loss(policy_values, rewards, old_values, self.value_clip)
                loss_kl = F.kl_div(action_logprobs, old_action_probs, reduction='batchmean')
                policy_loss = aux_loss + loss_kl

                update_network_(policy_loss, self.opt_actor)

                # paper says it is important to train the value network extra during the auxiliary phase
                values = self.critic(states)
                value_loss = clipped_value_loss(values, rewards, old_values, self.value_clip)

                update_network_(value_loss, self.opt_critic)
                           

    def train(self,env_name = 5555, num_episodes = 50000,max_steps = 500,update_steps = 5000,
              num_policy_updates_per_aux = 32,seed = None, save_every = 1000):

        # Load enviroment and buffers

        env = enviroment(env_name)

        memories = deque([])
        aux_memories = deque([])

        if exists(seed):
            torch.manual_seed(seed)
            np.random.seed(seed)

        current_steps = 0
        num_policy_updates = 0
        total_steps = 0


        for eps in range(1,num_episodes+1):
            mean_reward = []
            mean_time = []
            print('Episode ' + str(eps) + '/' + str(num_episodes))
            
            for steps in range(max_steps):

                # Perform rollouts
                state = env.state()
                start_time = time.time()
                total_steps += 1

                state_tensor = torch.from_numpy(np.asarray(state)).float()
                action_probs, _ = self.actor(state_tensor)
                value = self.critic(state_tensor)

                dist = Categorical(action_probs)
                action = dist.sample()
                action_log_prob = dist.log_prob(action)
                action = action.item()

                next_state, reward, done = env.step(action,state)
                memory = Memory(state, action, action_log_prob, reward, done, value)
                memories.append(memory)
                mean_reward.append(reward)

                mean_time.append(time.time() - start_time) 
                self.display_info(steps,max_steps,mean_reward,mean_time)

                # Update Policy and Value network

                if total_steps % update_steps == 0:    
                    self.learn(memories, aux_memories,next_state)
                    num_policy_updates += 1
                    memories.clear()

                    # Update Auxiliar network

                    if num_policy_updates % num_policy_updates_per_aux == 0:
                        self.learn_aux(aux_memories)
                        aux_memories.clear()

                # End of epochs via failure/success or max_steps

                if done:
                    print('Steps = ' + str(steps+1) +'/'+ str(max_steps) + ' | Mean Reward = ' + str(np.mean(mean_reward, dtype=np.float16)) + ' | Mean Iteration time = ' +str(np.mean(mean_time, dtype=np.float16))+ 's | Total Episode Time = ' + str(np.sum(mean_time, dtype=np.float16))+'s')
                    env.restart()
                    break          
                if total_steps % max_steps == 0:
                    env.reset()
                    break

            if eps % save_every == 0:
                self.save()

        env.close()
