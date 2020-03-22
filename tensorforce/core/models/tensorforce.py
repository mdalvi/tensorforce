# Copyright 2018 Tensorforce Team. All Rights Reserved.
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

from collections import OrderedDict

import tensorflow as tf

from tensorforce import TensorforceError, util
from tensorforce.core import memory_modules, Module, optimizer_modules, parameter_modules, \
    tf_function
from tensorforce.core.estimators import Estimator
from tensorforce.core.models import Model
from tensorforce.core.objectives import objective_modules
from tensorforce.core.policies import policy_modules


class TensorforceModel(Model):

    def __init__(
        self,
        # Model
        name, device, parallel_interactions, buffer_observe, seed, execution, saver, summarizer,
        config, states, actions, preprocessing, exploration, variable_noise,
        l2_regularization,
        # TensorforceModel
        policy, memory, update, optimizer, objective, reward_estimation, baseline_policy,
        baseline_optimizer, baseline_objective, entropy_regularization, max_episode_timesteps
    ):
        # Policy internals specification
        policy_cls, first_arg, kwargs = Module.get_module_class_and_kwargs(
            name='policy', module=policy, modules=policy_modules, states_spec=states,
            actions_spec=actions
        )
        if first_arg is None:
            internals = policy_cls.internals_spec(**kwargs)
        else:
            internals = policy_cls.internals_spec(first_arg, **kwargs)
        if any(internal.startswith('baseline-') for internal in internals):
            raise TensorforceError.value(
                name='model', argument='internals', value=list(internals),
                hint='starts with baseline-'
            )

        # Baseline internals specification
        if baseline_policy is None:
            pass
        else:
            baseline_cls, first_arg, kwargs = Module.get_module_class_and_kwargs(
                name='baseline', module=baseline_policy, modules=policy_modules, states_spec=states,
                actions_spec=actions
            )
            if first_arg is None:
                baseline_internals = baseline_cls.internals_spec(**kwargs)
            else:
                baseline_internals = baseline_cls.internals_spec(first_arg, **kwargs)
            for internal, spec in baseline_internals.items():
                if internal in internals:
                    raise TensorforceError.collision(
                        name='model', value='internals', group1='policy', group2='baseline'
                    )
                internals[internal] = spec

        super().__init__(
            # Model
            name=name, device=device, parallel_interactions=parallel_interactions,
            buffer_observe=buffer_observe, seed=seed, execution=execution, saver=saver,
            summarizer=summarizer, config=config, states=states, internals=internals,
            actions=actions, preprocessing=preprocessing, exploration=exploration,
            variable_noise=variable_noise, l2_regularization=l2_regularization
        )

        # Policy
        self.policy = self.add_module(
            name='policy', module=policy, modules=policy_modules, states_spec=self.states_spec,
            auxiliaries_spec=self.auxiliaries_spec, actions_spec=self.actions_spec
        )
        # assert 'internals' not in self.values_spec
        # self.values_spec['internals'] = self.policy.__class__.internals_spec(policy=self.policy)

        # Update mode
        if not all(key in ('batch_size', 'frequency', 'start', 'unit') for key in update):
            raise TensorforceError.value(
                name='agent', argument='update', value=list(update),
                hint='not from {batch_size,frequency,start,unit}'
            )
        # update: unit
        elif 'unit' not in update:
            raise TensorforceError.required(name='agent', argument='update[unit]')
        elif update['unit'] not in ('timesteps', 'episodes'):
            raise TensorforceError.value(
                name='agent', argument='update[unit]', value=update['unit'],
                hint='not in {timesteps,episodes}'
            )
        # update: batch_size
        elif 'batch_size' not in update:
            raise TensorforceError.required(name='agent', argument='update[batch_size]')

        self.update_unit = update['unit']
        self.update_batch_size = self.add_module(
            name='update_batch_size', module=update['batch_size'], modules=parameter_modules,
            is_trainable=False, dtype='long', min_value=1
        )
        if 'frequency' in update and update['frequency'] == 'never':
            self.update_frequency = None
        else:
            self.update_frequency = self.add_module(
                name='update_frequency', module=update.get('frequency', update['batch_size']),
                modules=parameter_modules, is_trainable=False, dtype='long', min_value=1,
                max_value=max(2, self.update_batch_size.max_value())
            )
            self.update_start = self.add_module(
                name='update_start', module=update.get('start', 0), modules=parameter_modules,
                is_trainable=False, dtype='long', min_value=0
            )

        # Optimizer
        self.optimizer = self.add_module(
            name='optimizer', module=optimizer, modules=optimizer_modules, is_trainable=False
        )

        # Objective
        self.objective = self.add_module(
            name='objective', module=objective, modules=objective_modules, is_trainable=False
        )

        # Baseline optimization overview:
        # Policy    Objective   Optimizer   Config
        #   n         n           n           estimate_horizon=False
        #   n         n           f           invalid!!!
        #   n         n           y           invalid!!!
        #   n         y           n           bl trainable, weighted 1.0
        #   n         y           f           bl trainable, weighted
        #   n         y           y           separate, use main policy
        #   y         n           n           bl trainable, estimate_advantage=True, equal horizon
        #   y         n           f           invalid!!!
        #   y         n           y           separate, use main objective
        #   y         y           n           bl trainable, weighted 1.0, equal horizon
        #   y         y           f           bl trainable, weighted, equal horizon
        #   y         y           y           separate

        # Baseline optimizer
        if baseline_optimizer is None:
            self.baseline_optimizer = None
            if baseline_objective is None:
                self.baseline_loss_weight = None
            else:
                self.baseline_loss_weight = 1.0
        elif isinstance(baseline_optimizer, float):
            assert baseline_objective is not None
            self.baseline_optimizer = None
            self.baseline_loss_weight = baseline_optimizer
        else:
            assert baseline_objective is not None or baseline_policy is not None
            self.baseline_optimizer = self.add_module(
                name='baseline_optimizer', module=baseline_optimizer, modules=optimizer_modules,
                is_subscope=True, is_trainable=False
            )
            self.baseline_loss_weight = None

        # Baseline
        if (baseline_policy is not None or baseline_objective is not None) and \
                self.baseline_optimizer is None:
            # since otherwise not part of training
            assert baseline_objective is not None or \
                reward_estimation.get('estimate_advantage', True)
            is_trainable = True
        else:
            is_trainable = False
        if baseline_policy is None:
            self.separate_baseline_policy = False
        else:
            self.separate_baseline_policy = True
            self.baseline = self.add_module(
                name='baseline', module=baseline_policy, modules=policy_modules, is_subscope=True,
                is_trainable=is_trainable, states_spec=self.states_spec,
                auxiliaries_spec=self.auxiliaries_spec, actions_spec=self.actions_spec
            )

        # Baseline objective
        if baseline_objective is None:
            self.baseline_objective = None
        else:
            self.baseline_objective = self.add_module(
                name='baseline_objective', module=baseline_objective, modules=objective_modules,
                is_subscope=True, is_trainable=False
            )

        # Estimator
        if not all(key in (
            'discount', 'estimate_actions', 'estimate_advantage', 'estimate_horizon',
            'estimate_terminal', 'horizon'
        ) for key in reward_estimation):
            raise TensorforceError.value(
                name='agent', argument='reward_estimation', value=reward_estimation,
                hint='not from {discount,estimate_actions,estimate_advantage,estimate_horizon,'
                     'estimate_terminal,horizon}'
            )
        if not self.separate_baseline_policy and self.baseline_optimizer is None and \
                self.baseline_objective is None:
            estimate_horizon = False
        else:
            estimate_horizon = 'late'
        if self.separate_baseline_policy and self.baseline_objective is None and \
                self.baseline_optimizer is None:
            estimate_advantage = True
        else:
            estimate_advantage = False
        if self.separate_baseline_policy:
            max_past_horizon = self.baseline.max_past_horizon(on_policy=False)
        else:
            max_past_horizon = self.policy.max_past_horizon(on_policy=False)
        self.estimator = self.add_module(
            name='estimator', module=Estimator, is_trainable=False,  # is_subscope=True,
            is_saved=False, values_spec=self.values_spec, horizon=reward_estimation['horizon'],
            discount=reward_estimation.get('discount', 1.0),
            estimate_horizon=reward_estimation.get('estimate_horizon', estimate_horizon),
            estimate_actions=reward_estimation.get('estimate_actions', False),
            estimate_terminal=reward_estimation.get('estimate_terminal', False),
            estimate_advantage=reward_estimation.get('estimate_advantage', estimate_advantage),
            # capacity=reward_estimation['capacity']
            min_capacity=self.buffer_observe, max_past_horizon=max_past_horizon
        )

        # Memory
        if self.update_unit == 'timesteps':
            max_past_horizon = self.policy.max_past_horizon(on_policy=True)
            if self.separate_baseline_policy:
                max_past_horizon = max(
                    max_past_horizon, (
                        self.baseline.max_past_horizon(on_policy=True) - \
                        self.estimator.min_future_horizon()
                    )
                )
            min_capacity = self.update_batch_size.max_value() + 1 + max_past_horizon + \
                self.estimator.max_future_horizon()
        elif self.update_unit == 'episodes':
            if max_episode_timesteps is None:
                min_capacity = 0
            else:
                min_capacity = (self.update_batch_size.max_value() + 1) * max_episode_timesteps 
        else:
            assert False

        self.memory = self.add_module(
            name='memory', module=memory, modules=memory_modules, is_trainable=False,
            values_spec=self.values_spec, min_capacity=min_capacity
        )

        # Entropy regularization
        entropy_regularization = 0.0 if entropy_regularization is None else entropy_regularization
        self.entropy_regularization = self.add_module(
            name='entropy_regularization', module=entropy_regularization,
            modules=parameter_modules, is_trainable=False, dtype='float', min_value=0.0
        )

        # Internals initialization
        self.internals_init.update(self.policy.internals_init())
        if self.separate_baseline_policy:
            self.internals_init.update(self.baseline.internals_init())
        if any(internal_init is None for internal_init in self.internals_init.values()):
            raise TensorforceError.required(name='model', argument='internals_init')

    def input_signature(self, function):
        if function == 'baseline_loss':
            return [
                util.to_tensor_spec(value_spec=self.states_spec, batched=True),
                util.to_tensor_spec(value_spec=dict(type='long', shape=(2,)), batched=True),
                util.to_tensor_spec(value_spec=self.internals_spec, batched=True),
                util.to_tensor_spec(value_spec=self.auxiliaries_spec, batched=True),
                util.to_tensor_spec(value_spec=self.actions_spec, batched=True),
                util.to_tensor_spec(value_spec=dict(type='float', shape=()), batched=True)
            ]

        elif function == 'core_experience':
            return [
                util.to_tensor_spec(value_spec=self.states_spec, batched=True),
                util.to_tensor_spec(value_spec=self.internals_spec, batched=True),
                util.to_tensor_spec(value_spec=self.auxiliaries_spec, batched=True),
                util.to_tensor_spec(value_spec=self.actions_spec, batched=True),
                util.to_tensor_spec(value_spec=dict(type='long', shape=()), batched=True),
                util.to_tensor_spec(value_spec=dict(type='float', shape=()), batched=True)
            ]

        elif function == 'core_update':
            return list()

        elif function == 'optimize':
            return [util.to_tensor_spec(value_spec=dict(type='long', shape=()), batched=True)]

        elif function == 'optimize_baseline':
            return [util.to_tensor_spec(value_spec=dict(type='long', shape=()), batched=True)]

        elif function == 'total_loss':
            return [
                util.to_tensor_spec(value_spec=self.states_spec, batched=True),
                util.to_tensor_spec(value_spec=dict(type='long', shape=(2,)), batched=True),
                util.to_tensor_spec(value_spec=self.internals_spec, batched=True),
                util.to_tensor_spec(value_spec=self.auxiliaries_spec, batched=True),
                util.to_tensor_spec(value_spec=self.actions_spec, batched=True),
                util.to_tensor_spec(value_spec=dict(type='float', shape=()), batched=True)
            ]

        else:
            return super().input_signature(function=function)

    def tf_initialize(self):
        super().tf_initialize()

        # Actions
        self.actions_input = OrderedDict()
        for name, action_spec in self.actions_spec.items():
            self.actions_input[name] = self.add_placeholder(
                name=name, dtype=action_spec['type'], shape=action_spec['shape'], batched=True
            )

        # Last update
        self.last_update = self.add_variable(
            name='last-update', dtype='long', shape=(), initializer=-1, is_trainable=False
        )

    def api_experience(self):
        # Inputs
        states = OrderedDict(self.states_input)
        internals = OrderedDict(self.internals_input)
        auxiliaries = OrderedDict(self.auxiliaries_input)
        actions = OrderedDict(self.actions_input)
        terminal = self.terminal_input
        reward = self.reward_input

        zero = tf.constant(value=0, dtype=util.tf_dtype(dtype='long'))
        true = tf.constant(value=True, dtype=util.tf_dtype(dtype='bool'))
        batch_size = tf.shape(input=terminal)[:1]

        # Assertions
        assertions = list()
        # terminal: type and shape
        tf.debugging.assert_type(
            tensor=terminal, tf_type=util.tf_dtype(dtype='long'),
            message="Agent.experience: invalid type for terminal input."
        )
        assertions.append(tf.debugging.assert_rank(
            x=terminal, rank=1, message="Agent.experience: invalid shape for terminal input."
        ))
        # reward: type and shape
        tf.debugging.assert_type(
            tensor=reward, tf_type=util.tf_dtype(dtype='float'),
            message="Agent.experience: invalid type for reward input."
        )
        assertions.append(tf.debugging.assert_rank(
            x=reward, rank=1, message="Agent.experience: invalid shape for reward input."
        ))
        # shape of terminal equals shape of reward
        assertions.append(tf.debugging.assert_equal(
            x=tf.shape(input=terminal), y=tf.shape(input=reward),
            message="Agent.experience: incompatible shapes of terminal and reward input."
        ))
        # buffer index is zero
        assertions.append(tf.debugging.assert_equal(
            x=tf.math.reduce_sum(input_tensor=self.buffer_index, axis=0),
            y=tf.constant(value=0, dtype=util.tf_dtype(dtype='long')),
            message="Agent.experience: cannot be called mid-episode."
        ))
        # at most one terminal
        assertions.append(tf.debugging.assert_less_equal(
            x=tf.math.count_nonzero(input=terminal, dtype=util.tf_dtype(dtype='long')),
            y=tf.constant(value=1, dtype=util.tf_dtype(dtype='long')),
            message="Agent.experience: input contains more than one terminal."
        ))
        # if terminal, last timestep in batch
        assertions.append(tf.debugging.assert_equal(
            x=tf.math.reduce_any(input_tensor=tf.math.greater(x=terminal, y=zero)),
            y=tf.math.greater(x=terminal[-1], y=zero),
            message="Agent.experience: terminal is not the last input timestep."
        ))
        # states: type and shape
        for name, spec in self.states_spec.items():
            tf.debugging.assert_type(
                tensor=states[name], tf_type=util.tf_dtype(dtype=spec['type']),
                message="Agent.experience: invalid type for {} state input.".format(name)
            )
            shape = tf.constant(
                value=self.unprocessed_state_shape.get(name, spec['shape']),
                dtype=util.tf_dtype(dtype='int')
            )
            assertions.append(
                tf.debugging.assert_equal(
                    x=tf.shape(input=states[name], out_type=util.tf_dtype(dtype='int')),
                    y=tf.concat(values=(batch_size, shape), axis=0),
                    message="Agent.experience: invalid shape for {} state input.".format(name)
                )
            )
        # internals: type and shape
        for name, spec in self.internals_spec.items():
            tf.debugging.assert_type(
                tensor=internals[name], tf_type=util.tf_dtype(dtype=spec['type']),
                message="Agent.experience: invalid type for {} internal input.".format(name)
            )
            shape = tf.constant(value=spec['shape'], dtype=util.tf_dtype(dtype='int'))
            assertions.append(
                tf.debugging.assert_equal(
                    x=tf.shape(input=internals[name], out_type=util.tf_dtype(dtype='int')),
                    y=tf.concat(values=(batch_size, shape), axis=0),
                    message="Agent.experience: invalid shape for {} internal input.".format(name)
                )
            )
        # action_masks: type and shape
        for name, spec in self.actions_spec.items():
            if spec['type'] == 'int':
                name = name + '_mask'
                tf.debugging.assert_type(
                    tensor=auxiliaries[name], tf_type=util.tf_dtype(dtype='bool'),
                    message="Agent.experience: invalid type for {} action-mask input.".format(name)
                )
                shape = tf.constant(
                    value=(spec['shape'] + (spec['num_values'],)), dtype=util.tf_dtype(dtype='int')
                )
                assertions.append(
                    tf.debugging.assert_equal(
                        x=tf.shape(input=auxiliaries[name], out_type=util.tf_dtype(dtype='int')),
                        y=tf.concat(values=(batch_size, shape), axis=0),
                        message="Agent.experience: invalid shape for {} action-mask input.".format(
                            name
                        )
                    )
                )
                assertions.append(
                    tf.debugging.assert_equal(
                        x=tf.reduce_all(
                            input_tensor=tf.reduce_any(
                                input_tensor=auxiliaries[name], axis=(len(spec['shape']) + 1)
                            ), axis=tuple(range(len(spec['shape']) + 1))
                        ),
                        y=true, message="Agent.experience: at least one action has to be valid "
                                        "for {} action-mask input.".format(name)
                    )
                )
        # actions: type and shape
        for name, spec in self.actions_spec.items():
            tf.debugging.assert_type(
                tensor=actions[name], tf_type=util.tf_dtype(dtype=spec['type']),
                message="Agent.experience: invalid type for {} action input.".format(name)
            )
            shape = tf.constant(value=spec['shape'], dtype=util.tf_dtype(dtype='int'))
            assertions.append(
                tf.debugging.assert_equal(
                    x=tf.shape(input=actions[name], out_type=util.tf_dtype(dtype='int')),
                    y=tf.concat(values=(batch_size, shape), axis=0),
                    message="Agent.experience: invalid shape for {} action input.".format(name)
                )
            )

        # Set global tensors
        self.set_global_tensor(
            name='independent', tensor=tf.constant(value=False, dtype=util.tf_dtype(dtype='bool'))
        )
        self.set_global_tensor(
            name='deterministic', tensor=tf.constant(value=True, dtype=util.tf_dtype(dtype='bool'))
        )

        with tf.control_dependencies(control_inputs=assertions):
            # Core experience: retrieve experience operation
            experienced = self.core_experience(
                states=states, internals=internals, auxiliaries=auxiliaries, actions=actions,
                terminal=terminal, reward=reward
            )

        with tf.control_dependencies(control_inputs=(experienced,)):
            # Function-level identity operation for retrieval (plus enforce dependency)
            timestep = util.identity_operation(
                x=self.global_tensor(name='timestep'), operation_name='timestep-output'
            )
            episode = util.identity_operation(
                x=self.global_tensor(name='episode'), operation_name='episode-output'
            )
            update = util.identity_operation(
                x=self.global_tensor(name='update'), operation_name='update-output'
            )

        return timestep, episode, update

    def api_update(self):
        # Set global tensors
        self.set_global_tensor(
            name='independent', tensor=tf.constant(value=False, dtype=util.tf_dtype(dtype='bool'))
        )
        self.set_global_tensor(
            name='deterministic', tensor=tf.constant(value=True, dtype=util.tf_dtype(dtype='bool'))
        )

        # Core update: retrieve update operation
        updated = self.core_update()

        with tf.control_dependencies(control_inputs=(updated,)):
            # Function-level identity operation for retrieval (plus enforce dependency)
            timestep = util.identity_operation(
                x=self.global_tensor(name='timestep'), operation_name='timestep-output'
            )
            episode = util.identity_operation(
                x=self.global_tensor(name='episode'), operation_name='episode-output'
            )
            update = util.identity_operation(
                x=self.global_tensor(name='update'), operation_name='update-output'
            )

        return timestep, episode, update

    @tf_function(num_args=3)
    def core_act(self, states, internals, auxiliaries):
        zero = tf.constant(value=0, dtype=util.tf_dtype(dtype='long'))

        # Dependency horizon
        past_horizon = self.policy.past_horizon(on_policy=False)
        if self.separate_baseline_policy:
            past_horizon = tf.math.maximum(
                x=past_horizon, y=self.baseline.past_horizon(on_policy=False)
            )

        # TODO: handle arbitrary non-optimization horizons!
        # assertion = tf.debugging.assert_equal(
        #     x=past_horizon, y=zero,
        #     message="Temporary: policy and baseline cannot depend on previous states unless "
        #             "optimization."
        # )
        # with tf.control_dependencies(control_inputs=(assertion,)):
        some_state = next(iter(states.values()))
        if util.tf_dtype(dtype='long') in (tf.int32, tf.int64):
            batch_size = tf.shape(input=some_state, out_type=util.tf_dtype(dtype='long'))[0]
        else:
            batch_size = tf.dtypes.cast(
                x=tf.shape(input=some_state)[0], dtype=util.tf_dtype(dtype='long')
            )
        starts = tf.range(start=batch_size, dtype=util.tf_dtype(dtype='long'))
        lengths = tf.ones(shape=(batch_size,), dtype=util.tf_dtype(dtype='long'))
        horizons = tf.stack(values=(starts, lengths), axis=1)

        # Policy act
        policy_internals = {
            name: internals[name]
            for name in self.policy.__class__.internals_spec(policy=self.policy)
        }
        actions, next_internals = self.policy.act(
            states=states, horizons=horizons, internals=policy_internals, auxiliaries=auxiliaries,
            return_internals=True
        )

        if self.separate_baseline_policy and \
                len(self.baseline.__class__.internals_spec(policy=self.baseline)) > 0:
            # Baseline policy act to retrieve next internals
            baseline_internals = {
                name: internals[name] for name in self.baseline.__class__.internals_spec(
                    policy=self.baseline
                )
            }
            _, baseline_internals = self.baseline.act(
                states=states, horizons=horizons, internals=baseline_internals,
                auxiliaries=auxiliaries, return_internals=True
            )
            assert all(name not in next_internals for name in baseline_internals)
            next_internals.update(baseline_internals)

        return actions, next_internals

    @tf_function(num_args=6)
    def core_observe(self, states, internals, auxiliaries, actions, terminal, reward):
        zero = tf.constant(value=0, dtype=util.tf_dtype(dtype='long'))
        one = tf.constant(value=1, dtype=util.tf_dtype(dtype='long'))

        # Experience
        experienced = self.core_experience(
            states=states, internals=internals, auxiliaries=auxiliaries, actions=actions,
            terminal=terminal, reward=reward
        )

        # If no periodic update
        if self.update_frequency is None:
            return experienced

        # Periodic update
        with tf.control_dependencies(control_inputs=(experienced,)):
            batch_size = self.update_batch_size.value()
            frequency = self.update_frequency.value()
            start = self.update_start.value()

            if self.update_unit == 'timesteps':
                # Timestep-based batch
                past_horizon = self.policy.past_horizon(on_policy=True)
                if self.separate_baseline_policy:
                    past_horizon = tf.math.maximum(
                        x=past_horizon, y=(
                            self.baseline.past_horizon(on_policy=True) - \
                            self.estimator.future_horizon()
                        )
                    )
                future_horizon = self.estimator.future_horizon()
                start = tf.math.maximum(
                    x=start, y=(frequency + past_horizon + future_horizon + one)
                )
                unit = self.global_tensor(name='timestep')

            elif self.update_unit == 'episodes':
                # Episode-based batch
                start = tf.math.maximum(x=start, y=frequency)
                unit = self.global_tensor(name='episode')

            unit = unit - start
            is_frequency = tf.math.equal(x=tf.math.mod(x=unit, y=frequency), y=zero)
            is_frequency = tf.math.logical_and(x=is_frequency, y=(unit > self.last_update))

            def perform_update():
                assignment = self.last_update.assign(value=unit, read_value=False)
                with tf.control_dependencies(control_inputs=(assignment,)):
                    return self.core_update()

            is_updated = self.cond(
                pred=is_frequency, true_fn=perform_update, false_fn=util.no_operation
            )

        return is_updated

    @tf_function(num_args=6)
    def core_experience(self, states, internals, auxiliaries, actions, terminal, reward):
        zero = tf.constant(value=0, dtype=util.tf_dtype(dtype='long'))

        # Enqueue experience for early reward estimation
        if self.separate_baseline_policy:
            any_overwritten, overwritten_values = self.estimator.enqueue(
                states=states, internals=internals, auxiliaries=auxiliaries, actions=actions,
                terminal=terminal, reward=reward, baseline=self.baseline
            )
        else:
            any_overwritten, overwritten_values = self.estimator.enqueue(
                states=states, internals=internals, auxiliaries=auxiliaries, actions=actions,
                terminal=terminal, reward=reward, baseline=self.policy
            )

        # If terminal, store remaining values in memory

        def true_fn():
            if self.separate_baseline_policy:
                reset_values = self.estimator.reset(baseline=self.baseline)
            else:
                reset_values = self.estimator.reset(baseline=self.policy)

            new_overwritten_values = OrderedDict()
            for name, value1, value2 in util.zip_items(overwritten_values, reset_values):
                if util.is_nested(name=name):
                    new_overwritten_values[name] = OrderedDict()
                    for inner_name, value1, value2 in util.zip_items(value1, value2):
                        new_overwritten_values[name][inner_name] = tf.concat(
                            values=(value1, value2), axis=0
                        )
                else:
                    new_overwritten_values[name] = tf.concat(values=(value1, value2), axis=0)
            return new_overwritten_values

        def false_fn():
            return overwritten_values

        with tf.control_dependencies(control_inputs=util.flatten(xs=overwritten_values)):
            values = self.cond(pred=(terminal[-1] > zero), true_fn=true_fn, false_fn=false_fn)

        # If any, store overwritten values
        def store():
            return self.memory.enqueue(**values)

        terminal = values['terminal']
        if util.tf_dtype(dtype='long') in (tf.int32, tf.int64):
            num_values = tf.shape(input=terminal, out_type=util.tf_dtype(dtype='long'))[0]
        else:
            num_values = tf.dtypes.cast(
                x=tf.shape(input=terminal)[0], dtype=util.tf_dtype(dtype='long')
            )

        stored = self.cond(pred=(num_values > zero), true_fn=store, false_fn=util.no_operation)

        return stored

    @tf_function(num_args=0)
    def core_update(self):
        true = tf.constant(value=True, dtype=util.tf_dtype(dtype='bool'))
        one = tf.constant(value=1, dtype=util.tf_dtype(dtype='long'))

        # Retrieve batch
        batch_size = self.update_batch_size.value()
        if self.update_unit == 'timesteps':
            # Timestep-based batch
            # Dependency horizon
            past_horizon = self.policy.past_horizon(on_policy=True)
            if self.separate_baseline_policy:
                past_horizon = tf.math.maximum(
                    x=past_horizon, y=self.baseline.past_horizon(on_policy=True)
                )
            future_horizon = self.estimator.future_horizon()
            indices = self.memory.retrieve_timesteps(
                n=batch_size, past_horizon=past_horizon, future_horizon=future_horizon
            )
        elif self.update_unit == 'episodes':
            # Episode-based batch
            indices = self.memory.retrieve_episodes(n=batch_size)

        # Optimization
        optimized = self.optimize(indices=indices)

        # Increment update
        with tf.control_dependencies(control_inputs=(optimized,)):
            assignment = self.update.assign_add(delta=one, read_value=False)

        with tf.control_dependencies(control_inputs=(assignment,)):
            return util.identity_operation(x=true)

    @tf_function(num_args=1)
    def optimize(self, indices):
        # Baseline optimization
        if self.baseline_optimizer is not None:
            optimized = self.optimize_baseline(indices=indices)
            dependencies = (optimized,)
        else:
            dependencies = (indices,)

        # Reward estimation
        with tf.control_dependencies(control_inputs=dependencies):
            reward = self.memory.retrieve(indices=indices, values='reward')
            if self.separate_baseline_policy:
                reward = self.estimator.complete(
                    indices=indices, reward=reward, baseline=self.baseline,
                    memory=self.memory
                )
            else:
                reward = self.estimator.complete(
                    indices=indices, reward=reward, baseline=self.policy, memory=self.memory
                )
            reward = self.add_summary(
                label=('empirical-reward', 'rewards'), name='empirical-reward', tensor=reward
            )
            is_baseline_optimized = self.separate_baseline_policy and \
                self.baseline_optimizer is None and self.baseline_objective is None
            if self.separate_baseline_policy:
                reward = self.estimator.estimate(
                    indices=indices, reward=reward, baseline=self.baseline,
                    memory=self.memory, is_baseline_optimized=is_baseline_optimized
                )
            else:
                reward = self.estimator.estimate(
                    indices=indices, reward=reward, baseline=self.policy, memory=self.memory,
                    is_baseline_optimized=is_baseline_optimized
                )
            reward = self.add_summary(
                label=('estimated-reward', 'rewards'), name='estimated-reward', tensor=reward
            )

        # Stop gradients of estimated rewards if separate baseline optimization
        if not is_baseline_optimized:
            reward = tf.stop_gradient(input=reward)

        # Retrieve states, internals and actions
        past_horizon = self.policy.past_horizon(on_policy=True)
        if self.separate_baseline_policy and self.baseline_optimizer is None:
            assertion = tf.debugging.assert_equal(
                x=past_horizon,
                y=self.baseline.past_horizon(on_policy=True),
                message="Policy and baseline depend on a different number of previous states."
            )
        else:
            assertion = past_horizon

        with tf.control_dependencies(control_inputs=(assertion,)):
            # horizon change: see timestep-based batch sampling
            horizons, states, internals = self.memory.predecessors(
                indices=indices, horizon=past_horizon, sequence_values='states',
                initial_values='internals'
            )
            auxiliaries, actions = self.memory.retrieve(
                indices=indices, values=('auxiliaries', 'actions')
            )

        # Optimizer arguments
        independent = self.set_global_tensor(
            name='independent', tensor=tf.constant(value=True, dtype=util.tf_dtype(dtype='bool'))
        )

        variables = self.trainable_variables

        arguments = dict(
            states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries,
            actions=actions, reward=reward
        )

        fn_loss = self.total_loss

        def fn_kl_divergence(states, horizons, internals, auxiliaries, actions, reward, other=None):
            kl_divergence = self.policy.kl_divergence(
                states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries,
                other=other
            )
            if self.separate_baseline_policy and self.baseline_optimizer is None and \
                    self.baseline_objective is not None:
                kl_divergence += self.baseline.kl_divergence(
                    states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries,
                    other=other
                )
            return kl_divergence

        if self.global_model is None:
            global_variables = None
        else:
            global_variables = self.global_model.trainable_variables

        kwargs = self.objective.optimizer_arguments(policy=self.policy, baseline=self.baseline)
        if self.separate_baseline_policy and self.baseline_optimizer is None and \
                self.baseline_objective is not None:
            util.deep_disjoint_update(
                target=kwargs,
                source=self.baseline_objective.optimizer_arguments(policy=self.baseline)
            )

        dependencies = util.flatten(xs=arguments)

        # KL divergence before
        if self.is_summary_logged(
            label=('kl-divergence', 'action-kl-divergences', 'kl-divergences')
        ):
            with tf.control_dependencies(control_inputs=dependencies):
                kldiv_reference = self.policy.kldiv_reference(
                    states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries
                )
                dependencies = util.flatten(xs=kldiv_reference)

        # Optimization
        with tf.control_dependencies(control_inputs=dependencies):
            optimized = self.optimizer.minimize(
                variables=variables, arguments=arguments, fn_loss=fn_loss,
                fn_kl_divergence=fn_kl_divergence, global_variables=global_variables, **kwargs
            )

        with tf.control_dependencies(control_inputs=(optimized,)):
            # Loss summaries
            if self.is_summary_logged(label=('loss', 'objective-loss', 'losses')):
                objective_loss = self.objective.loss_per_instance(policy=self.policy, **arguments)
                objective_loss = tf.math.reduce_mean(input_tensor=objective_loss, axis=0)
            if self.is_summary_logged(label=('objective-loss', 'losses')):
                optimized = self.add_summary(
                    label=('objective-loss', 'losses'), name='objective-loss',
                    tensor=objective_loss, pass_tensors=optimized
                )
            if self.is_summary_logged(label=('loss', 'regularization-loss', 'losses')):
                regularization_loss = self.regularize(
                    states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries
                )
            if self.is_summary_logged(label=('regularization-loss', 'losses')):
                optimized = self.add_summary(
                    label=('regularization-loss', 'losses'), name='regularization-loss',
                    tensor=regularization_loss, pass_tensors=optimized
                )
            if self.is_summary_logged(label=('loss', 'losses')):
                loss = objective_loss + regularization_loss
            if self.baseline_optimizer is None:
                if self.is_summary_logged(label=('loss', 'baseline-objective-loss', 'losses')):
                    if self.baseline_objective is None:
                        if self.separate_baseline_policy:
                            baseline_objective_loss = self.objective.loss_per_instance(
                                policy=self.baseline, **arguments
                            )
                    elif self.separate_baseline_policy:
                        baseline_objective_loss = self.baseline_objective.loss_per_instance(
                            policy=self.baseline, **arguments
                        )
                    else:
                        baseline_objective_loss = self.baseline_objective.loss_per_instance(
                            policy=self.policy, **arguments
                        )
                    baseline_objective_loss = tf.math.reduce_mean(
                        input_tensor=baseline_objective_loss, axis=0
                    )
                if self.is_summary_logged(label=('baseline-objective-loss', 'losses')):
                    optimized = self.add_summary(
                        label=('baseline-objective-loss', 'losses'),
                        name='baseline-objective-loss', tensor=baseline_objective_loss,
                        pass_tensors=optimized
                    )
                if self.separate_baseline_policy and self.is_summary_logged(
                    label=('loss', 'baseline-regularization-loss', 'losses')
                ):
                    baseline_regularization_loss = self.baseline.regularize()
                if self.is_summary_logged(label=('baseline-regularization-loss', 'losses')):
                    optimized = self.add_summary(
                        label=('baseline-regularization-loss', 'losses'),
                        name='baseline-regularization-loss', tensor=baseline_regularization_loss,
                        pass_tensors=optimized
                    )
                if self.is_summary_logged(label=('loss', 'baseline-loss', 'losses')):
                    baseline_loss = baseline_objective_loss + baseline_regularization_loss
                if self.is_summary_logged(label=('baseline-loss', 'losses')):
                    optimized = self.add_summary(
                        label=('baseline-loss', 'losses'), name='baseline-loss',
                        tensor=baseline_loss, pass_tensors=optimized
                    )
                if self.is_summary_logged(label=('loss', 'losses')):
                    loss += self.baseline_loss_weight * baseline_loss
            if self.is_summary_logged(label=('loss', 'losses')):
                optimized = self.add_summary(
                    label=('loss', 'losses'), name='loss', tensor=loss, pass_tensors=optimized
                )

            # Entropy summaries
            if self.is_summary_logged(label=('entropy', 'action-entropies', 'entropies')):
                entropies = self.policy.entropy(
                    states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries,
                    include_per_action=(len(self.actions_spec) > 1)
                )
            if self.is_summary_logged(label=('entropy', 'entropies')):
                if len(self.actions_spec) == 1:
                    optimized = self.add_summary(
                        label=('entropy', 'entropies'), name='entropy', tensor=entropies,
                        pass_tensors=optimized
                    )
                else:
                    optimized = self.add_summary(
                        label=('entropy', 'entropies'), name='entropy', tensor=entropies['*'],
                        pass_tensors=optimized
                    )
            if len(self.actions_spec) > 1 and \
                    self.is_summary_logged(label=('action-entropies', 'entropies')):
                for name in self.actions_spec:
                    optimized = self.add_summary(
                        label=('action-entropies', 'entropies'), name=(name + '-entropy'),
                        tensor=entropies[name], pass_tensors=optimized
                    )

            # KL divergence summaries
            if self.is_summary_logged(
                label=('kl-divergence', 'action-kl-divergences', 'kl-divergences')
            ):
                kl_divergences = self.policy.kl_divergence(
                    states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries,
                    other=kldiv_reference, include_per_action=(len(self.actions_spec) > 1)
                )
            if self.is_summary_logged(label=('kl-divergence', 'kl-divergences')):
                if len(self.actions_spec) == 1:
                    optimized = self.add_summary(
                        label=('kl-divergence', 'kl-divergences'), name='kl-divergence',
                        tensor=kl_divergences, pass_tensors=optimized
                    )
                else:
                    optimized = self.add_summary(
                        label=('kl-divergence', 'kl-divergences'), name='kl-divergence',
                        tensor=kl_divergences['*'], pass_tensors=optimized
                    )
            if len(self.actions_spec) > 1 and \
                    self.is_summary_logged(label=('action-kl-divergences', 'kl-divergences')):
                for name in self.actions_spec:
                    optimized = self.add_summary(
                        label=('action-kl-divergences', 'kl-divergences'),
                        name=(name + '-kl-divergence'), tensor=kl_divergences[name],
                        pass_tensors=optimized
                    )

        self.set_global_tensor(name='independent', tensor=independent)

        return optimized

    @tf_function(num_args=6)
    def total_loss(self, states, horizons, internals, auxiliaries, actions, reward, **kwargs):
        # Loss per instance
        loss = self.objective.loss_per_instance(
            policy=self.policy, states=states, horizons=horizons, internals=internals,
            auxiliaries=auxiliaries, actions=actions, reward=reward, **kwargs
        )

        # Objective loss
        loss = tf.math.reduce_mean(input_tensor=loss, axis=0)

        # Regularization losses
        loss += self.regularize(
            states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries
        )

        # Baseline loss
        if self.baseline_optimizer is None and self.baseline_objective is not None:
            loss += self.baseline_loss_weight * self.baseline_loss(
                states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries,
                actions=actions, reward=reward
            )
        else:
            assert self.baseline_loss_weight is None

        return loss

    @tf_function(num_args=4)
    def regularize(self, states, horizons, internals, auxiliaries):
        regularization_loss = super().regularize(
            states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries
        )

        # Entropy regularization
        zero = tf.constant(value=0.0, dtype=util.tf_dtype(dtype='float'))
        entropy_regularization = self.entropy_regularization.value()

        def no_entropy_regularization():
            return zero

        def apply_entropy_regularization():
            entropy = self.policy.entropy(
                states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries
            )
            entropy = tf.math.reduce_mean(input_tensor=entropy, axis=0)
            return -entropy_regularization * entropy

        skip_entropy_regularization = tf.math.equal(x=entropy_regularization, y=zero)
        regularization_loss += self.cond(
            pred=skip_entropy_regularization, true_fn=no_entropy_regularization,
            false_fn=apply_entropy_regularization
        )

        return regularization_loss

    @tf_function(num_args=1)
    def optimize_baseline(self, indices):
        # Retrieve states, internals, actions and reward
        past_horizon = self.baseline.past_horizon(on_policy=True)
        # horizon change: see timestep-based batch sampling
        horizons, states, internals = self.memory.predecessors(
            indices=indices, horizon=past_horizon, sequence_values='states',
            initial_values='internals'
        )
        auxiliaries, actions, reward = self.memory.retrieve(
            indices=indices, values=('auxiliaries', 'actions', 'reward')
        )

        # Reward estimation (separate from main policy, so updated baseline is used there)
        reward = self.memory.retrieve(indices=indices, values='reward')
        reward = self.estimator.complete(
            indices=indices, reward=reward, baseline=self.baseline, memory=self.memory
        )

        # Optimizer arguments
        independent = self.set_global_tensor(
            name='independent', tensor=tf.constant(value=True, dtype=util.tf_dtype(dtype='bool'))
        )

        variables = self.baseline.trainable_variables

        arguments = dict(
            states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries,
            actions=actions, reward=reward
        )

        fn_loss = self.baseline_loss

        def fn_kl_divergence(states, horizons, internals, auxiliaries, actions, reward, other=None):
            return self.baseline.kl_divergence(
                states=states, horizons=horizons, internals=internals, auxiliaries=auxiliaries,
                other=other
            )

        source_variables = self.policy.trainable_variables

        if self.global_model is None:
            global_variables = None
        else:
            global_variables = self.global_model.baseline_policy.trainable_variables

        if self.baseline_objective is None:
            kwargs = self.objective.optimizer_arguments(policy=self.baseline)
        else:
            kwargs = self.baseline_objective.optimizer_arguments(policy=self.baseline)

        # Optimization
        optimized = self.baseline_optimizer.minimize(
            variables=variables, arguments=arguments, fn_loss=fn_loss,
            fn_kl_divergence=fn_kl_divergence, source_variables=source_variables,
            global_variables=global_variables, **kwargs
        )

        with tf.control_dependencies(control_inputs=(optimized,)):
            # Loss summaries
            if self.is_summary_logged(
                label=('baseline-loss', 'baseline-objective-loss', 'losses')
            ):
                if self.baseline_objective is None:
                    objective_loss = self.objective.loss_per_instance(
                        policy=self.baseline, **arguments
                    )
                else:
                    objective_loss = self.baseline_objective.loss_per_instance(
                        policy=self.baseline, **arguments
                    )
                objective_loss = tf.math.reduce_mean(input_tensor=objective_loss, axis=0)
            if self.is_summary_logged(label=('baseline-objective-loss', 'losses')):
                optimized = self.add_summary(
                    label=('baseline-objective-loss', 'losses'), name='baseline-objective-loss',
                    tensor=objective_loss, pass_tensors=optimized
                )
            if self.is_summary_logged(
                label=('baseline-loss', 'baseline-regularization-loss', 'losses')
            ):
                regularization_loss = self.baseline.regularize()
            if self.is_summary_logged(label=('baseline-regularization-loss', 'losses')):
                optimized = self.add_summary(
                    label=('baseline-regularization-loss', 'losses'),
                    name='baseline-regularization-loss', tensor=regularization_loss,
                    pass_tensors=optimized
                )
            if self.is_summary_logged(label=('baseline-loss', 'losses')):
                loss = objective_loss + regularization_loss
                optimized = self.add_summary(
                    label=('baseline-loss', 'losses'), name='baseline-loss', tensor=loss,
                    pass_tensors=optimized
                )

        independent = self.set_global_tensor(name='independent', tensor=independent)

        return optimized

    @tf_function(num_args=6)
    def baseline_loss(self, states, horizons, internals, auxiliaries, actions, reward, **kwargs):
        # Loss per instance
        if self.baseline_objective is None:
            loss = self.objective.loss_per_instance(
                policy=self.baseline, states=states, horizons=horizons, internals=internals,
                auxiliaries=auxiliaries, actions=actions, reward=reward, **kwargs
            )
        else:
            loss = self.baseline_objective.loss_per_instance(
                policy=self.baseline, states=states, horizons=horizons, internals=internals,
                auxiliaries=auxiliaries, actions=actions, reward=reward, **kwargs
            )

        # Objective loss
        loss = tf.math.reduce_mean(input_tensor=loss, axis=0)

        # Regularization losses
        loss += self.baseline.regularize()

        return loss
