# Copyright 2017 reinforce.io. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division
import tensorflow as tf

from six.moves import xrange

import unittest

from tensorforce import Configuration
from tensorforce.agents import DQFDAgent
from tensorforce.core.networks import layered_network_builder
from tensorforce.environments.minimal_test import MinimalTest
from tensorforce.execution import Runner


class TestDQFDAgent(unittest.TestCase):

    def test_dqfd_agent(self):
        environment = MinimalTest(continuous=False)

        config = Configuration(
            batch_size=8,
            memory_capacity=20,
            first_update=20,
            repeat_update=4,
            target_update_frequency=1,
            states=environment.states,
            actions=environment.actions,
            learning_rate=0.00004,
            expert_sampling_ratio=0.01,
            supervised_weight=0.5,
            expert_margin=1
        )

        tf.reset_default_graph()

        # DQFD uses l2-reg
        network_builder = layered_network_builder(layers_config=[{'type': 'dense', 'size': 32,
                                                                  'weights_regularizer': 'tensorflow.contrib.layers.python.layers.regularizers.l2_regularizer',
                                                                  'weights_regularizer_kwargs': {
                                                                      'scale': 0.001
                                                                  }
                                                                  }])

        agent = DQFDAgent(config=config, network_builder=network_builder)

        # First: generate some data to add to demo memory
        state = environment.reset()
        agent.reset()

        for n in xrange(50):
            action = agent.act(state=state)
            state, step_reward, terminal = environment.execute(action=action)

            agent.add_demo_observation(state=state, action=action, reward=step_reward, terminal=terminal)

            if terminal:
                state = environment.reset()
                agent.reset()

        # Pre-train from demo data
        agent.pre_train(10000)

        # If pre-training works, we should not need much more training
        runner = Runner(agent=agent, environment=environment)

        def episode_finished(r):
            return r.episode < 100 or not all(x >= 1.0 for x in r.episode_rewards[-100:])

        runner.run(episodes=500, episode_finished=episode_finished)
        self.assertTrue(runner.episode < 500)
