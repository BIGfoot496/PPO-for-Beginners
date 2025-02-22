"""
    The file contains the PPO class to train with.
    NOTE: All "ALG STEP"s are following the numbers from the original PPO pseudocode.
            It can be found here: https://spinningup.openai.com/en/latest/_images/math/e62a8971472597f4b014c2da064f636ffe365ba3.svg
"""

import gymnasium as gym
import time
import wandb

import numpy as np
import torch
import torch.nn as nn
from torch.optim import Adam
from torch.optim.lr_scheduler import ExponentialLR
from torch.distributions import MultivariateNormal, Categorical

from matplotlib import pyplot as plt

from rnd import RND

class PPO:
    """
        This is the PPO class we will use as our model in main.py
    """
    def __init__(self, actor, critic, env, **hyperparameters):
        """
            Initializes the PPO model, including hyperparameters.
            Parameters:
                actor, critic - pre-built architectures of the actor and critic networks 
                    Both have to have the input be of obs_shape 
                    The actor must output act_shape and the critic must output (1,) 
                env - the environment to train on.
                hyperparameters - all extra arguments passed into PPO that should be hyperparameters.
            Returns:
                None
        """

        # Initialize hyperparameters for training with PPO
        self._init_hyperparameters(hyperparameters)
                
        # Make sure the environment is compatible with our code
        assert(type(env.observation_space) == gym.spaces.Box)
            
        # Extract environment information
        self.env = env
        self.obs_shape = env.observation_space.shape

        if type(env.action_space) == gym.spaces.Box:
            self.act_type = 'box'
            self.act_shape = env.action_space.shape
        
        if type(env.action_space) == gym.spaces.Discrete:
            self.act_type = 'discrete'
            # For discrete spaces the action is a softmax vector of probabilities to take each action
            self.act_shape = (env.action_space.n,)

        # Initialize actor and critic networks
        self.actor = actor                                                                                      # ALG STEP 1
        self.critic = critic

        # Initialize optimizers for actor and critic
        self.actor_optim = Adam(self.actor.parameters(), lr=self.lr, eps=1e-5)
        self.critic_optim = Adam(self.critic.parameters(), lr=self.lr, eps=1e-5)
        
        # Initialize learning rate schedulers
        self.actor_scheduler = ExponentialLR(self.actor_optim, gamma=self.annealing_rate)
        self.critic_scheduler = ExponentialLR(self.critic_optim, gamma=self.annealing_rate)
        
        if self.act_type == 'box':
            # Initialize the covariance matrix used to query the actor for actions
            self.cov_var = torch.full(size=self.act_shape, fill_value=0.5)
            self.cov_mat = torch.diag(self.cov_var)
        
        # This logger will help us with printing out summaries of each iteration
        self.logger = {
            'delta_t': time.time_ns(),
            't_so_far': 0,          # timesteps so far
            'i_so_far': 0,          # iterations so far
            'batch_lens': [],       # episodic lengths in batch
            'batch_rews': [],       # episodic returns in batch
            'actor_losses': [],     # losses of actor network in current iteration
        }
        
        # Walk a random agent through the environment to initialize the RND networks
        ex_f = self.exploration_factor
        self.exploration_factor = 0
        batch_obs, _, _, _, _, _ = self.rollout()
        self.rnd = RND(self.obs_shape, batch_obs.numpy())
        self.exploration_factor = ex_f

    def learn(self, total_timesteps):
        """
            Train the actor and critic networks. Here is where the main PPO algorithm resides.
            Parameters:
                total_timesteps - the total number of timesteps to train for
            Return:
                None
        """
        print(f"Learning... Running {self.max_timesteps_per_episode} timesteps per episode, ", end='')
        print(f"{self.timesteps_per_batch} timesteps per batch for a total of {total_timesteps} timesteps")
        t_so_far = 0 # Timesteps simulated so far
        i_so_far = 0 # Iterations ran so far
        while t_so_far < total_timesteps:                                                                       # ALG STEP 2
            # Autobots, roll out (just kidding, we're collecting our batch simulations here)
            batch_obs, batch_acts, batch_log_probs, batch_intr_rews, batch_extr_rews, batch_lens = self.rollout()    # ALG STEP 3

            # Calculate how many timesteps we collected this batch
            t_so_far += np.sum(batch_lens)

            # Increment the number of iterations
            i_so_far += 1

            # Logging timesteps so far and iterations so far
            self.logger['t_so_far'] = t_so_far
            self.logger['i_so_far'] = i_so_far

            # Calculate advantage at k-th iteration using GAE
            V, _ = self.evaluate(batch_obs, batch_acts)
            batch_rews = batch_extr_rews + self.exploration_factor*batch_intr_rews
            A_k = self.estimate_advantage(batch_rews, V.detach())

            # One of the only tricks I use that isn't in the pseudocode. Normalizing advantages
            # isn't theoretically necessary, but in practice it decreases the variance of 
            # our advantages and makes convergence much more stable and faster. I added this because
            # solving some environments was too unstable without it.
            A_k = (A_k - A_k.mean()) / (A_k.std() + 1e-10)

            # This is the loop where we update our network for some n epochs
            for _ in range(self.n_updates_per_iteration):                                                       # ALG STEP 6 & 7
                # Calculate V_phi and pi_theta(a_t | s_t)
                V, curr_log_probs = self.evaluate(batch_obs, batch_acts)

                # Calculate the ratio pi_theta(a_t | s_t) / pi_theta_k(a_t | s_t)
                ratios = torch.exp(curr_log_probs - batch_log_probs)

                # Calculate surrogate losses.
                surr1 = ratios * A_k
                surr2 = torch.clamp(ratios, 1 - self.clip, 1 + self.clip) * A_k

                # Calculate actor and critic losses.
                # NOTE: we take the negative min of the surrogate losses because we're trying to maximize
                # the performance function, but Adam minimizes the loss. So minimizing the negative
                # performance function maximizes it.
                actor_loss = (-torch.min(surr1, surr2)).mean()
                critic_loss = nn.MSELoss()(V, A_k + V)

                # Calculate gradients and perform backward propagation for actor network
                self.actor_optim.zero_grad()
                actor_loss.backward(retain_graph=True)
                self.actor_optim.step()

                # Calculate gradients and perform backward propagation for critic network
                self.critic_optim.zero_grad()
                critic_loss.backward()
                self.critic_optim.step()
                
                # Log actor loss
                self.logger['actor_losses'].append(actor_loss.detach())
                        
            # Anneal the learning rate
            self.actor_scheduler.step()
            self.critic_scheduler.step()
            self.rnd.anneal_lr()
            
            # Print a summary of our training so far
            self._log_summary()

            plt.scatter(np.transpose(batch_obs)[0].numpy(), np.transpose(batch_obs)[1].numpy())
            plt.show()

            # Save our model if it's time
            if i_so_far % self.save_freq == 0:
                torch.save(self.actor.state_dict(), './ppo_actor.pth')
                torch.save(self.critic.state_dict(), './ppo_critic.pth')

    def rollout(self):
        """
            Too many transformers references, I'm sorry. This is where we collect the batch of data
            from simulation. Since this is an on-policy algorithm, we'll need to collect a fresh batch
            of data each time we iterate the actor/critic networks.
            Parameters:
                None
            Return:
                batch_obs - the observations collected this batch. Shape: (number of timesteps, dimension of observation)
                batch_acts - the actions collected this batch. Shape: (number of timesteps, dimension of action)
                batch_log_probs - the log probabilities of each action taken this batch. Shape: (number of timesteps)
                batch_intr_rews - the intrinsic rewards of each timestep in this batch. Shape: (number of timesteps)
                batch_extr_rews - the extrinsic rewards of each timestep in this batch. Shape: (number of episodes)
                batch_lens - the lengths of each episode this batch. Shape: (number of episodes)
        """
        # Batch data. For more details, check function header.
        batch_obs = []
        batch_acts = []
        batch_log_probs = []
        batch_intr_rews = []
        batch_extr_rews = []
        batch_lens = []

        # Episodic data. Keeps track of rewards per episode, will get cleared
        # upon each new episode
        ep_intr_rews = []
        ep_extr_rews = []

        t = 0 # Keeps track of how many timesteps we've run so far this batch

        # Keep simulating until we've run more than or equal to specified timesteps per batch
        while t < self.timesteps_per_batch:
            ep_intr_rews = [] # rewards collected per episode
            ep_extr_rews = []

            # Reset the environment. sNote that obs is short for observation. 
            obs = self.env.reset()[0]
            done = False

            # Run an episode for a maximum of max_timesteps_per_episode timesteps
            for ep_t in range(self.max_timesteps_per_episode):
                # If render is specified, render the environment
                if self.render and (self.logger['i_so_far'] % self.render_every_i == 0) and len(batch_lens) == 0:
                    self.env.render()

                t += 1 # Increment timesteps ran this batch so far

                # Track observations in this batch
                batch_obs.append(obs)

                # Calculate action 
                action, log_prob = self.get_action(obs)
                
                # Make a step in the env and track reward.
                # Note that rew is short for reward.
                obs, rew, done, _, _ = self.env.step(action)
                ep_extr_rews.append(rew)
                if self.exploration_factor:
                    in_rew = self.rnd.get_reward(obs)
                    ep_intr_rews.append(in_rew)
                
                # Track recent action, and action log probability
                if self.act_type == 'discrete':
                    action = torch.tensor(action)
                batch_acts.append(action)
                batch_log_probs.append(log_prob)

                # If the environment tells us the episode is terminated, break
                if done:
                    break
                
            if self.exploration_factor:
                # Reset the variance estimator in RND
                if self.logger['i_so_far'] < self.std_set_iteration:
                    self.rnd.reset_rew_std(ep_intr_rews)
                
                # Normalize intrinsic rewards
                ep_intr_rews = ep_intr_rews / (self.rnd.get_rew_std() + 1e-10)
            
            # Track episodic lengths and rewards
            batch_lens.append(ep_t + 1)
            batch_intr_rews.append(ep_intr_rews)
            batch_extr_rews.append(ep_extr_rews)

        # Reshape data as tensors in the shape specified in function description, before returning
        batch_obs = torch.tensor(np.array(batch_obs), dtype=torch.float)
        batch_acts = torch.tensor(batch_acts, dtype=torch.float)
        batch_log_probs = torch.tensor(batch_log_probs, dtype=torch.float)
        batch_extr_rews = np.array(batch_extr_rews)
        batch_intr_rews = np.array(batch_intr_rews)
        if not self.exploration_factor:
            batch_intr_rews = np.zeros_like(batch_extr_rews)

        # Log the episodic returns and episodic lengths in this batch.
        self.logger['batch_intr_rews'] = batch_intr_rews
        self.logger['batch_extr_rews'] = batch_extr_rews
        self.logger['batch_lens'] = batch_lens

        return batch_obs, batch_acts, batch_log_probs, batch_intr_rews, batch_extr_rews, batch_lens

    # Generalized Advantage Estimation
    def estimate_advantage(self, batch_rews, values):
        """
            Estimate the advantage at each timestep in a batch given the rewards.
            Parameters:
                batch_rews - the rewards in a batch, Shape: (number of episodes, number of timesteps per episode)
                values - the value function estimates, Shape: (number of timesteps in batch)
            Return:
                advantages - the estimated advantages, Shape: (number of timesteps in batch)
        """
        # The advantages per episode per batch to return.
        # The shape will be (num timesteps per episode)
        advantages = []
        
        # Iterate through each episode
        for ep_rews in reversed(batch_rews):
            
            discounted_estimate = 0
            
            # Iterate through all rewards in the episode.
            for rew, v_cur, v_next in reversed(list(zip(ep_rews, values, values[1:]))):
                delta = rew + v_next * self.gamma - v_cur
                discounted_estimate = delta + discounted_estimate * self.gamma * self.lambda_return
                advantages.insert(0, discounted_estimate)

        # Convert the advantages into a tensor
        advantages = torch.tensor(advantages, dtype=torch.float)

        return advantages
        
    def get_action(self, obs):
        """
            Queries an action from the actor network, should be called from rollout.
            Parameters:
                obs - the observation at the current timestep
            Return:
                action - the action to take, as a numpy array
                log_prob - the log probability of the selected action in the distribution
        """
        # Query the actor network for a mean action
        out = self.actor(obs)
        
        if self.act_type == 'box':
            # Create a distribution with the mean action and std from the covariance matrix above.
            dist = MultivariateNormal(out, self.cov_mat)

        if self.act_type == 'discrete':
            # Create a distribution from the softmax vector the actor returned
            dist = Categorical(out)
        
        # Sample an action from the distribution
        action = dist.sample()
            
        # Calculate the log probability for that action
        log_prob = dist.log_prob(action)

        # Return the sampled action and the log probability of that action in our distribution
        return action.detach().numpy(), log_prob.detach()

    def evaluate(self, batch_obs, batch_acts):
        """
            Estimate the values of each observation, and the log probs of
            each action in the most recent batch with the most recent
            iteration of the actor network. Should be called from learn.
            Parameters:
                batch_obs - the observations from the most recently collected batch as a tensor.
                            Shape: (number of timesteps in batch, dimension of observation)
                batch_acts - the actions from the most recently collected batch as a tensor.
                            Shape: (number of timesteps in batch, dimension of action)
            Return:
                V - the predicted values of batch_obs
                log_probs - the log probabilities of the actions taken in batch_acts given batch_obs
        """
        # Query critic network for a value V for each batch_obs. Shape of V should be same as batch_rews
        V = self.critic(batch_obs).squeeze()

        # Calculate the log probabilities of batch actions using most recent actor network.
        # This segment of code is similar to that in get_action()
        out = self.actor(batch_obs)
        if self.act_type == 'box':
            dist = MultivariateNormal(out, self.cov_mat)
        if self.act_type == 'discrete':
            dist = Categorical(out)
        log_probs = dist.log_prob(batch_acts)

        # Return the value vector V of each observation in the batch
        # and log probabilities log_probs of each action in the batch
        return V, log_probs
    
    def _init_hyperparameters(self, hyperparameters):
        """
            Initialize default and custom values for hyperparameters
            Parameters:
                hyperparameters - the extra arguments included when creating the PPO model, should only include
                                    hyperparameters defined below with custom values.
            Return:
                None
        """
        # Initialize default values for hyperparameters
        # Algorithm hyperparameters
        self.timesteps_per_batch = 4800                 # Number of timesteps to run per batch
        self.max_timesteps_per_episode = 1600           # Max number of timesteps per episode
        self.n_updates_per_iteration = 5                # Number of times to update actor/critic per iteration
        self.lr = 0.005                                 # Learning rate of actor optimizer
        self.gamma = 0.95                               # Discount factor to be applied when calculating Rewards-To-Go
        self.lambda_return = 0.96                       # Smoothing factor to be applied in GAE. lambda=1 is equivalent to Monte Carlo
        self.clip = 0.2                                 # Recommended 0.2, helps define the threshold to clip the ratio during SGA
        self.annealing_rate = 0.995                     # Rate at which the learning rate drops to 0 with time
        self.exploration_factor = 1                     # This is beta from r = r_e + beta * r_i. If beta=0 curiosity is off.
        self.std_set_iteration = 3                      # The number of iterations until we think the ICM overfits, and the reward variance becomes stable.
        
        # Miscellaneous parameters
        self.render = True                              # If we should render during rollout
        self.render_every_i = 10                        # Only render every n iterations
        self.save_freq = 10                             # How often we save in number of iterations
        self.seed = None                                # Sets the seed of our program, used for reproducibility of results

        # Change any default values to custom values for specified hyperparameters
        for param, val in hyperparameters.items():
            exec('self.' + param + ' = ' + str(val))

        # Sets the seed if specified
        if self.seed != None:
            # Check if our seed is valid first
            assert(type(self.seed) == int)

            # Set the seed 
            torch.manual_seed(self.seed)
            print(f"Successfully set seed to {self.seed}")

    def _log_summary(self):
        """
            Print to stdout what we've logged so far in the most recent batch.
            Parameters:
                None
            Return:
                None
        """
        # Calculate logging values. I use a few python shortcuts to calculate each value
        # without explaining since it's not too important to PPO; feel free to look it over,
        # and if you have any questions you can email me (look at bottom of README)
        delta_t = self.logger['delta_t']
        self.logger['delta_t'] = time.time_ns()
        delta_t = (self.logger['delta_t'] - delta_t) / 1e9
        delta_t = str(round(delta_t, 2))

        t_so_far = self.logger['t_so_far']
        i_so_far = self.logger['i_so_far']
        avg_ep_lens = np.mean(self.logger['batch_lens'])
        avg_ep_extr_rews = np.mean([np.sum(ep_extr_rews) for ep_extr_rews in self.logger['batch_extr_rews']])
        avg_ep_intr_rews = np.mean([np.sum(ep_intr_rews) for ep_intr_rews in self.logger['batch_intr_rews']])
        avg_actor_loss = np.mean([losses.float().mean() for losses in self.logger['actor_losses']])
        lr = self.actor_scheduler.get_last_lr()[0]

        # Log the data in W&B
        wandb.log({"length": avg_ep_lens, "reward": avg_ep_extr_rews, "intrinsic reward":avg_ep_intr_rews, "loss": avg_actor_loss})

        # Round decimal places for more aesthetic logging messages
        avg_ep_lens = str(round(avg_ep_lens, 2))
        avg_ep_extr_rews = str(round(avg_ep_extr_rews, 2))
        avg_ep_intr_rews = str(round(avg_ep_intr_rews, 2))
        avg_actor_loss = str(round(avg_actor_loss, 5))

        # Print logging statements
        print(flush=True)
        print(f"-------------------- Iteration #{i_so_far} --------------------", flush=True)
        print(f"Learning rate: {lr}", flush=True)
        print(f"Average Episodic Length: {avg_ep_lens}", flush=True)
        print(f"Average Episodic Return: {avg_ep_extr_rews}", flush=True)
        print(f"Average Episodic Intrinsic Reward: {avg_ep_intr_rews}", flush=True)
        print(f"Average Loss: {avg_actor_loss}", flush=True)
        print(f"Timesteps So Far: {t_so_far}", flush=True)
        print(f"Iteration took: {delta_t} secs", flush=True)
        print("------------------------------------------------------", flush=True)
        print(flush=True)

        # Reset batch-specific logging data
        self.logger['batch_lens'] = []
        self.logger['batch_rews'] = []
        self.logger['actor_losses'] = []
