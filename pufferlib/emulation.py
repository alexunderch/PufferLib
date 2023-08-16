from pdb import set_trace as T

import numpy as np
import warnings

import gym
import inspect
from collections import OrderedDict, Mapping

import pufferlib
from pufferlib import utils, exceptions

from .cext import flatten, unflatten


class Postprocessor:
    '''Base class for user-defined featurizers and postprocessors
    
    Instantiated per agent or team'''
    def __init__(self, env):
        '''Provides full access to the underlying environment'''
        self.env = env

    def reset(self, obs):
        '''Called at the beginning of each episode'''
        return

    def features(self, obs):
        '''Called on each observation after it is returned by the environment'''
        return obs

    def actions(self, actions):
        '''Called on each action before it is passed to the environment'''
        return actions

    def rewards_dones_infos(self, rewards, dones, infos):
        '''Called on each reward, done, and info after they are returned by the environment'''
        return rewards, dones, infos


class BasicPostprocessor(Postprocessor):
    '''Basic postprocessor that injects returns and lengths information into infos and
    provides an option to pad to a maximum episode length. Works for single-agent and
    team-based multi-agent environments'''
    def reset(self, obs):
        self.epoch_return = 0
        self.epoch_length = 0
        self.done = False

    def rewards_dones_infos(self, rewards, dones, infos):
        if isinstance(rewards, Mapping):
            rewards = sum(rewards.values())

        # Env is done
        if len(self.env.agents) == 0:
            infos['return'] = self.epoch_return
            infos['length'] = self.epoch_length
            self.done = True
        elif not dones:
            self.epoch_length += 1
            self.epoch_return += rewards

        return infos


class GymPufferEnv:
    def __init__(self, env=None, env_creator=None, env_args=[], env_kwargs={},
            postprocessor_cls=Postprocessor):
        self.env = make_object(env, env_creator, env_args, env_kwargs)
        self.postprocessor = postprocessor_cls(self.env)

        self.initialized = False
        self.done = True

        # Cache the observation and action spaces
        self.observation_space
        self.action_space

    @property
    def observation_space(self):
        '''Returns a flattened, single-tensor observation space'''

        # Call user featurizer and create a corresponding gym space
        self.structured_observation_space, structured_ob = make_featurized_obs_and_space(
            self.env.observation_space, self.postprocessor)

        # Flatten the featurized observation space and store
        # it for use in step. Return a box space for the user
        self.flat_observation_space, self.box_observation_space, self.pad_observation = make_flat_and_box_obs_space(
            self.structured_observation_space, structured_ob)

        return self.box_observation_space

    @property
    def action_space(self):
        '''Returns a flattened, multi-discrete action space'''
        self.structured_action_space = self.env.action_space

        # Store a flat version of the action space for use in step. Return a multidiscrete version for the user
        self.flat_action_space, multi_discrete_action_space = make_flat_and_multidiscrete_atn_space(self.env.action_space)

        return multi_discrete_action_space

    def reset(self, seed=None):
        self.initialized = True
        self.done = False

        ob = _seed_and_reset(self.env, seed)

        # Call user featurizer and flatten the observations
        return postprocess_and_flatten(
            ob, self.postprocessor, reset=True)

    def step(self, action):
        '''Execute an action and return (observation, reward, done, info)'''
        if not self.initialized:
            raise exceptions.APIUsageError('step() called before reset()')
        if self.done:
            raise exceptions.APIUsageError('step() called after environment is done')
 
        action = self.postprocessor.actions(action)

        if __debug__ and not self.action_space.contains(action):
            raise ValueError(f'Action:\n{action}\n '
                f'not in space:\n{self.action_space}')

        # Unpack actions from multidiscrete into the original action space
        action = unflatten(
            split(
                action, self.flat_action_space, batched=False
            )
        )

        ob, reward, done, info = self.env.step(action)
        self.done = done

        # Call user postprocessors and flatten the observations
        processed_ob, single_reward, single_done, single_info = postprocess_and_flatten(
            ob, self.postprocessor, reward, done, info)

        if __debug__ and not self.observation_space.contains(processed_ob):
            raise ValueError(f'Observation:\n{processed_ob}\n '
                f'not in space:\n{self.observation_space}')

        return processed_ob, single_reward, single_done, single_info

    def close(self):
        return self.env.close()


class PettingZooPufferEnv:
    def __init__(self, env=None, env_creator=None, env_args=[], env_kwargs={},
                 postprocessor_cls=Postprocessor, teams=None):
        self.env = make_object(env, env_creator, env_args, env_kwargs)
        self.initialized = False

        self.possible_agents = self.env.possible_agents if teams is None else list(teams.keys())
        self.teams = teams

        self.postprocessors = {agent: postprocessor_cls(self.env)
            for agent in self.possible_agents}

        # Cache the observation and action spaces
        self.observation_space(self.possible_agents[0])
        self.action_space(self.possible_agents[0])

    @property
    def agents(self):
        return self.env.agents

    @property
    def done(self):
        return len(self.agents) == 0

    @property
    def single_observation_space(self):
        return self.box_observation_space

    @property
    def single_action_space(self):
        return self.multidiscrete_action_space

    def observation_space(self, agent):
        '''Returns the observation space for a single agent'''
        if agent not in self.possible_agents:
            raise pufferlib.exceptions.InvalidAgentError(agent, self.possible_agents)

        # Make a gym space defining observations for the whole team
        if self.teams is not None:
            obs_space = make_team_space(
                self.env.observation_space, self.teams[agent])
        else:
            obs_space = self.env.observation_space(agent)

        # Call user featurizer and create a corresponding gym space
        self.structured_observation_space, structured_obs = make_featurized_obs_and_space(
            obs_space, self.postprocessors[agent])

        # Flatten the featurized observation space and store it for use in step. Return a box space for the user
        self.flat_observation_space, self.box_observation_space, self.pad_observation = make_flat_and_box_obs_space(
            self.structured_observation_space, structured_obs)

        return self.box_observation_space 

    def action_space(self, agent):
        '''Returns the action space for a single agent'''
        if agent not in self.possible_agents:
            raise pufferlib.exceptions.InvalidAgentError(agent, self.possible_agents)

        # Make a gym space defining actions for the whole team
        if self.teams is not None:
            atn_space = make_team_space(
                self.env.action_space, self.teams[agent])
        else:
            atn_space = self.env.action_space(agent)

        self.structured_action_space = atn_space

        # Store a flat version of the action space for use in step. Return a multidiscrete version for the user
        self.flat_action_space, self.multidiscrete_action_space = make_flat_and_multidiscrete_atn_space(atn_space)

        return self.multidiscrete_action_space

    def reset(self, seed=None):
        obs = self.env.reset(seed=seed)
        self.initialized = True

        # Group observations into teams
        if self.teams is not None:
            obs = group_into_teams(self.teams, obs)

        # Call user featurizer and flatten the observations
        postprocessed_obs = {}
        for agent in self.possible_agents:
            postprocessed_obs[agent] = postprocess_and_flatten(
                obs[agent], self.postprocessors[agent], reset=True)
            
        return postprocessed_obs

    def step(self, actions):
        '''Step the environment and return (observations, rewards, dones, infos)'''
        if not self.initialized:
            raise exceptions.APIUsageError('step() called before reset()')
        if self.done:
            raise exceptions.APIUsageError('step() called after environment is done')
        if __debug__:
            for agent, atn in actions.items():
                if agent not in self.possible_agents:
                    raise exceptions.InvalidAgentError(agent, self.agents)

        # Postprocess actions and validate action spaces
        for agent in actions:
            actions[agent] = self.postprocessors[agent].actions(actions[agent])

        if __debug__:
            check_spaces(actions, self.action_space)

        # Unpack actions from multidiscrete into the original action space
        unpacked_actions = {}
        for agent, atn in actions.items():
            if agent in self.agents:
                unpacked_actions[agent] = unflatten(
                    split(atn, self.flat_action_space, batched=False)
                )

        if self.teams is not None:
            unpacked_actions = ungroup_from_teams(self.teams, unpacked_actions)

        obs, rewards, dones, infos = self.env.step(unpacked_actions)

        if self.teams is not None:
            obs, rewards, dones = group_into_teams(self.teams, obs, rewards, dones)

        # Call user postprocessors and flatten the observations
        for agent in obs:
            obs[agent], rewards[agent], dones[agent], infos[agent] = postprocess_and_flatten(
                obs[agent], self.postprocessors[agent],
                rewards[agent], dones[agent], infos[agent])

        obs, rewards, dones, infos = pad_to_const_num_agents(
            self.env.possible_agents, obs, rewards, dones, infos, self.pad_observation)

        if __debug__:
            check_spaces(obs, self.observation_space)

        return obs, rewards, dones, infos

    def close(self):
        return self.env.close()


def make_object(object_instance=None, object_creator=None, creator_args=[], creator_kwargs={}):
    if (object_instance is None) == (object_creator is None):
        raise ValueError('Exactly one of object_instance or object_creator must be provided')

    if object_instance is not None:
        if callable(object_instance) or inspect.isclass(object_instance):
            raise TypeError('object_instance must be an instance, not a function or class')
        return object_instance

    if object_creator is not None:
        if not callable(object_creator):
            raise TypeError('object_creator must be a callable')
        
        if creator_args is None:
            creator_args = []

        if creator_kwargs is None:
            creator_kwargs = {}

        return object_creator(*creator_args, **creator_kwargs)


def pad_agent_data(data, agents, pad_value):
    return {agent: data[agent] if agent in data else pad_value
        for agent in agents}
    
def pad_to_const_num_agents(agents, obs, rewards, dones, infos, pad_obs):
    padded_obs = pad_agent_data(obs, agents, pad_obs)
    rewards = pad_agent_data(rewards, agents, 0)
    dones = pad_agent_data(dones, agents, False)
    infos = pad_agent_data(infos, agents, {})
    return padded_obs, rewards, dones, infos

def postprocess_and_flatten(ob, postprocessor,
        reward=None, done=None, info=None,
        reset=False, max_horizon=None):
    if reset:
        postprocessor.reset(ob)
    else:
        reward, done, info = postprocessor.rewards_dones_infos(
            reward, done, info)

    postprocessed_ob = postprocessor.features(ob)
    flat_ob = concatenate(flatten(postprocessed_ob))

    if reset:
        return flat_ob
    return flat_ob, reward, done, info


def make_flat_and_multidiscrete_atn_space(atn_space):
    flat_action_space = flatten_space(atn_space)
    multidiscrete_space = convert_to_multidiscrete(flat_action_space)
    return flat_action_space, multidiscrete_space


def make_flat_and_box_obs_space(obs_space, obs):
    flat_observation_space = flatten_space(obs_space)  
    flat_observation = concatenate(flatten(obs))

    mmin, mmax = pufferlib.utils._get_dtype_bounds(flat_observation.dtype)
    pad_obs = flat_observation * 0
    box_obs_space = gym.spaces.Box(
        low=mmin, high=mmax,
        shape=flat_observation.shape, dtype=flat_observation.dtype
    )

    return flat_observation_space, box_obs_space, pad_obs


def make_featurized_obs_and_space(obs_space, postprocessor):
    obs_sample = obs_space.sample()
    featurized_obs = postprocessor.features(obs_sample)
    featurized_obs_space = make_space_like(featurized_obs)
    return featurized_obs_space, featurized_obs

def make_team_space(observation_space, agents):
    return gym.spaces.Dict({agent: observation_space(agent) for agent in agents})

def check_spaces(data, spaces):
    for k, v in data.items():
        try:
            contains = spaces(k).contains(v)
        except:
            raise ValueError(
                f'Error checking space {spaces} for agent/team {k} with data:\n{v}')

        if not contains:
            raise ValueError(
                f'Data:\n{v}\n for agent/team {k} not in '
                f'space:\n{spaces(k)}')

def check_teams(env, teams):
    if set(env.possible_agents) != {item for team in teams.values() for item in team}:
        raise ValueError(f'Invalid teams: {teams} for possible_agents: {env.possible_agents}')

def group_into_teams(teams, *args):
    grouped_data = []

    for agent_data in args:
        if __debug__ and set(agent_data) != {item for team in teams.values() for item in team}:
            raise ValueError(f'Invalid teams: {teams} for agents: {set(agent_data)}')

        team_data = {}
        for team_id, team in teams.items():
            team_data[team_id] = {}
            for agent_id in team:
                if agent_id in agent_data:
                    team_data[team_id][agent_id] = agent_data[agent_id]

        grouped_data.append(team_data)

    if len(grouped_data) == 1:
        return grouped_data[0]

    return grouped_data

def ungroup_from_teams(team_data):
    agent_data = {}
    for team in team_data.values():
        for agent_id, data in team.items():
            agent_data[agent_id] = data
    return agent_data

def flatten_space(space):
    def _recursion_helper(current, key):
        if isinstance(current, gym.spaces.Tuple):
            for idx, elem in enumerate(current):
                _recursion_helper(elem, f'{key}T{idx}.')
        elif isinstance(current, gym.spaces.Dict):
            for k, value in current.items():
                _recursion_helper(value, f'{key}D{k}.')
        else:
            flat[f'{key}V'] = current

    flat = {}
    _recursion_helper(space, '')
    return flat

def python_flatten(sample):
    def _recursion_helper(current):
        if isinstance(current, tuple):
            for elem in current:
                _recursion_helper(elem)
        elif isinstance(current, (dict, OrderedDict)):
            for value in current.values():
                _recursion_helper(value)
        elif isinstance(current, np.ndarray):
            flat.append(current)
        else:
            flat.append(np.array([current]))

    flat = []
    _recursion_helper(sample)
    return flat

def python_unflatten(flat_sample, space):
    idx = [0]  # Wrapping the index in a list to maintain the reference
    def _recursion_helper(space):
        if isinstance(space, gym.spaces.Tuple):
            unflattened_tuple = tuple(_recursion_helper(subspace) for subspace in space)
            return unflattened_tuple
        if isinstance(space, gym.spaces.Dict):
            unflattened_dict = OrderedDict((key, _recursion_helper(subspace)) for key, subspace in space.items())
            return unflattened_dict
        if isinstance(space, gym.spaces.Discrete):
            idx[0] += 1
            return int(flat_sample[idx[0] - 1])

        idx[0] += 1
        return flat_sample[idx[0] - 1]

    return _recursion_helper(space)

def concatenate(flat_sample):
    if len(flat_sample) == 1:
        return list(flat_sample.values())[0]
    return np.concatenate([
        e.ravel() if isinstance(e, np.ndarray) else np.array([e])
        for e in flat_sample.values()]
    )

def split(stacked_sample, flat_space, batched=True):
    if batched:
        batch = stacked_sample.shape[0]

    leaves = {}
    ptr = 0
    for key, subspace in flat_space.items():
        shape = subspace.shape
        typ = subspace.dtype
        sz = int(np.prod(shape))

        if shape == ():
            shape = (1,)

        if batched:
            samp = stacked_sample[:, ptr:ptr+sz].reshape(batch, *shape)
        else:
            samp = stacked_sample[ptr:ptr+sz].reshape(*shape).astype(typ)
            if isinstance(subspace, gym.spaces.Discrete):
                samp = int(samp[0])

        leaves[key] = samp
        ptr += sz

    return leaves

def unpack_batched_obs(batched_obs, flat_space):
    unpacked = split(batched_obs, flat_space, batched=True)
    unflattened = unflatten(unpacked)
    return unflattened

def convert_to_multidiscrete(flat_space):
    lens = []
    for e in flat_space.values():
        if isinstance(e, gym.spaces.Discrete):
            lens.append(e.n)
        elif isinstance(e, gym.spaces.MultiDiscrete):
            lens += e.nvec.tolist()
        else:
            raise ValueError(f'Invalid action space: {e}')

    return gym.spaces.MultiDiscrete(lens)

def make_space_like(ob):
    if type(ob) == np.ndarray:
        mmin, mmax = utils._get_dtype_bounds(ob.dtype)
        return gym.spaces.Box(
            low=mmin, high=mmax,
            shape=ob.shape, dtype=ob.dtype
        )

    # TODO: Handle Discrete (how to get max?)
    if type(ob) in (tuple, list):
        return gym.spaces.Tuple([make_space_like(v) for v in ob])

    if type(ob) in (dict, OrderedDict):
        return gym.spaces.Dict({k: make_space_like(v) for k, v in ob.items()})

    if type(ob) in (int, float):
        # TODO: Tighten bounds
        return gym.spaces.Box(low=-np.inf, high=np.inf, shape=())

    raise ValueError(f'Invalid type for featurized obs: {type(ob)}')
    
def _seed_and_reset(env, seed):
    try:
        env.seed(seed)
        old_seed=True
    except:
        old_seed=False

    if old_seed:
        obs = env.reset()
    else:
        try:
            obs = env.reset(seed=seed)
        except:
            obs= env.reset()
            warnings.warn('WARNING: Environment does not support seeding.', DeprecationWarning)

    return obs