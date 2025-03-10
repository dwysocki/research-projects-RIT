# -*- coding: utf-8 -*-
'''
Monte Carlo Integrator
----------------------
Perform an adaptive monte carlo integral.
'''
from __future__ import print_function
import numpy as np
from . import gaussian_mixture_model as GMM
import traceback
import time


try:
    from multiprocess import Pool
except:
    print('no multiprocess')


class integrator:
    '''
    Class to iteratively perform an adaptive Monte Carlo integral where the integrand
    is a combination of one or more Gaussian curves, in one or more dimensions.

    Parameters
    ----------
    d : int
        Total number of dimensions.

    bounds : np.ndarray
        Limits of integration, where each row represents [left_lim, right_lim]
        for its corresponding dimension.

    gmm_dict : dict
        Dictionary where each key is a tuple of one or more dimensions
        that are to be modeled together. If the integrand has strong correlations between
        two or more dimensions, they should be grouped. Each value is by default initialized
        to None, and is replaced with the GMM object for its dimension(s).

    n_comp : int or {tuple:int}
        The number of Gaussian components per group of dimensions. If its type is int,
        this number of components is used for all dimensions. If it is a dict, it maps
        each key in gmm_dict to an integer number of mixture model components.

    n : int
        Number of samples per iteration

    prior : function
        Function to evaluate prior for samples

    user_func : function
        Function to run each iteration

    L_cutoff : float
        Likelihood cutoff for samples to store

    use_lnL : bool
        Whether or not lnL or L will be returned by the integrand
    '''

    def __init__(self, d, bounds, gmm_dict, n_comp, n=None, prior=None,
                user_func=None, proc_count=None, L_cutoff=None, use_lnL=False,gmm_epsilon=None):
        # user-specified parameters
        self.d = d
        self.bounds = bounds
        self.gmm_dict = gmm_dict
        self.gmm_epsilon= gmm_epsilon
        self.n_comp = n_comp
        self.user_func=user_func
        self.prior = prior
        self.proc_count = proc_count
        self.use_lnL = use_lnL
        # constants
        self.t = 0.02 # percent estimated error threshold
        if n is None:
            self.n = int(5000 * self.d) # number of samples per batch
        else:
            self.n = int(n)
        self.ntotal = 0
        # integrator object parameters
        self.sample_array = None
        self.value_array = None
        self.sampling_prior_array = None
        self.prior_array = None
        self.scaled_error_squared = 0.
        self.log_error_scale_factor = 0.
        self.integral = 0
        self.eff_samp = 0
        self.iterations = 0 # for weighted averages and count
        self.max_value = float('-inf') # for calculating eff_samp
        self.total_value = 0 # for calculating eff_samp
        self.n_max = float('inf')
        # saved values
        self.cumulative_samples = np.empty((0, d))
        self.cumulative_values = np.empty(0)
        self.cumulative_p = np.empty(0)
        self.cumulative_p_s = np.empty(0)
        if L_cutoff is None:
            self.L_cutoff = -1
        else:
            self.L_cutoff = L_cutoff
        
    def _calculate_prior(self):
        if self.prior is None:
            self.prior_array = np.ones(self.n)
        else:
            self.prior_array = self.prior(self.sample_array).flatten()

    def _sample(self):
        self.sampling_prior_array = np.ones(self.n)
        self.sample_array = np.empty((self.n, self.d))
        for dim_group in self.gmm_dict: # iterate over grouped dimensions
            # create a matrix of the left and right limits for this set of dimensions
            new_bounds = np.empty((len(dim_group), 2))
            index = 0
            for dim in dim_group:
                new_bounds[index] = self.bounds[dim]
                index += 1
            model = self.gmm_dict[dim_group]
            if model is None:
                # sample uniformly for this group of dimensions
                llim = new_bounds[:,0]
                rlim = new_bounds[:,1]
                temp_samples = np.random.uniform(llim, rlim, (self.n, len(dim_group)))
                # update responsibilities
                vol = np.prod(rlim - llim)
                self.sampling_prior_array *= 1.0 / vol
            else:
                # sample from the gmm
                temp_samples = model.sample(self.n)#, new_bounds)
                # update responsibilities
                self.sampling_prior_array *= model.score(temp_samples)#, new_bounds)
            index = 0
            for dim in dim_group:
                # put columns of temp_samples in final places in sample_array
                self.sample_array[:,dim] = temp_samples[:,index]
                index += 1

    def _train(self):
        sample_array, value_array, sampling_prior_array = np.copy(self.sample_array), np.copy(self.value_array), np.copy(self.sampling_prior_array)
        if self.use_lnL:
            lnL = value_array
        else:
            lnL = np.log(value_array)
        log_weights = lnL + np.log(self.prior_array) - sampling_prior_array
        for dim_group in self.gmm_dict: # iterate over grouped dimensions
            # create a matrix of the left and right limits for this set of dimensions
            new_bounds = np.empty((len(dim_group), 2))
            index = 0
            for dim in dim_group:
                new_bounds[index] = self.bounds[dim]
                index += 1
            model = self.gmm_dict[dim_group] # get model for this set of dimensions
            temp_samples = np.empty((self.n, len(dim_group)))
            index = 0
            for dim in dim_group:
                # get samples corresponding to the current model
                temp_samples[:,index] = sample_array[:,dim]
                index += 1
            if model is None:
                # model doesn't exist yet
                if isinstance(self.n_comp, int) and self.n_comp != 0:
                    model = GMM.gmm(self.n_comp, new_bounds,epsilon=gmm_epsilon)
                    model.fit(temp_samples, log_sample_weights=log_weights)
                elif isinstance(self.n_comp, dict) and self.n_comp[dim_group] != 0:
                    model = GMM.gmm(self.n_comp[dim_group], new_bounds,epsilon=gmm_epsilon)
                    model.fit(temp_samples, log_sample_weights=log_weights)
            else:
                model.update(temp_samples, log_sample_weights=log_weights)
            self.gmm_dict[dim_group] = model


    def _calculate_results(self):
        if self.use_lnL:
            lnL = np.copy(self.value_array) # changing the naming convention, just for this function, now that I know better
        else:
            lnL = np.log(self.value_array)
        
        # strip off any samples with likelihoods less than our cutoff
        mask = lnL > (np.log(self.L_cutoff) if self.L_cutoff > 0 else -np.inf)
        lnL = lnL[mask]
        prior = self.prior_array[mask]
        sampling_prior = self.sampling_prior_array[mask]
        
        # append to the cumulative arrays
        self.cumulative_samples = np.append(self.cumulative_samples, self.sample_array[mask], axis=0)
        self.cumulative_values = np.append(self.cumulative_values, lnL[mask], axis=0)
        self.cumulative_p = np.append(self.cumulative_p, prior, axis=0)
        self.cumulative_p_s = np.append(self.cumulative_p_s, sampling_prior, axis=0)
        
        # compute the log sample weights
        log_weights = lnL + np.log(prior) - np.log(sampling_prior)
        
        # do a shift so that the highest log weight is 0, keeping track of the shift
        log_scale_factor = np.max(log_weights)
        scale_factor = np.exp(log_scale_factor)
        log_weights -= log_scale_factor
        summed_vals = scale_factor * np.sum(np.exp(log_weights))
        integral_value = summed_vals / self.n
        
        # Calculate the log of the Monte Carlo integration error.
        # Let `a` be the scale factor, and let `w` be the weights with the scale factor divided out (i.e. weight = a w), then
        #     error^2 = var(a w) / N = a^2 var(w) / N.
        # The point is that 0 <= w <= 1, so there's no potential for overflow here when calculating the variance.
        # Since the scale factor `a` is potentially very large, squaring it could overflow; therefore, we keep out a factor of ln(a^2) = 2ln(a).
        # Note that ln(a) is exactly the log_scale_factor variable defined above.
        scaled_error_squared = np.var(np.exp(log_weights)) / self.n
        log_error_scale_factor = 2. * log_scale_factor
        
        # calculate the running average of the integral
        self.integral = (self.iterations * self.integral + integral_value) / (self.iterations + 1)
        
        # Calculate the running average of the variance.
        # This calculation is complicated by the scale factors: the previous running average is scaled by exp(self.log_error_scale_factor),
        # which is currently the log_error_scale_factor from the previous iteration.
        # We can avoid overflows by factoring our *new* log_error_scale_factor out of the running average, so that we exponentiate a smaller number.
        self.scaled_error_squared = (self.iterations * np.exp(self.log_error_scale_factor - log_error_scale_factor) * self.scaled_error_squared + scaled_error_squared) / (self.iterations + 1)
        self.log_error_scale_factor = log_error_scale_factor # having computed the running average, update this
        
        # calculate the effective samples
        self.total_value += summed_vals
        self.max_value = max(scale_factor, self.max_value)
        self.eff_samp = self.total_value / self.max_value

    def _reset(self):
        ### reset GMMs
        for k in self.gmm_dict:
            self.gmm_dict[k] = None
        

    def integrate(self, func, min_iter=10, max_iter=20, var_thresh=0.0, max_err=10,
            neff=float('inf'), nmax=None, progress=False, epoch=None,verbose=True):
        '''
        Evaluate the integral

        Parameters
        ----------
        func : function
            Integrand function
        min_iter : int
            Minimum number of integrator iterations
        max_iter : int
            Maximum number of integrator iterations
        var_thresh : float
            Variance threshold for terminating integration
        max_err : int
            Maximum number of errors to catch before terminating integration
        neff : float
            Effective samples threshold for terminating integration
        nmax : int
            Maximum number of samples to draw
        progress : bool
            Print GMM parameters each iteration
        '''
        err_count = 0
        cumulative_eval_time = 0
        if nmax is None:
            nmax = max_iter * self.n
        while self.iterations < max_iter and self.ntotal < nmax and self.eff_samp < neff:
#            print('Iteration:', self.iterations)
            if err_count >= max_err:
                print('Exiting due to errors...')
                break
            try:
                self._sample()
            except KeyboardInterrupt:
                print('KeyboardInterrupt, exiting...')
                break
            except Exception as e:
                print(traceback.format_exc())
                print('Error sampling, resetting...')
                err_count += 1
                self._reset()
                continue
            t1 = time.time()
            if self.proc_count is None:
                self.value_array = func(np.copy(self.sample_array)).flatten()
            else:
                split_samples = np.array_split(self.sample_array, self.proc_count)
                p = Pool(self.proc_count)
                self.value_array = np.concatenate(p.map(func, split_samples), axis=0)
                p.close()
            cumulative_eval_time += time.time() - t1
            self._calculate_prior()
            self._calculate_results()
            self.iterations += 1
            self.ntotal += self.n
            if self.iterations >= min_iter and np.log(self.scaled_error_squared) + self.log_error_scale_factor < np.log(var_thresh):
                break
            try:
                self._train()
            except KeyboardInterrupt:
                print('KeyboardInterrupt, exiting...')
                break
            except Exception as e:
                print(traceback.format_exc())
                print('Error training, resetting...')
                err_count += 1
                self._reset()
            if self.user_func is not None:
                self.user_func(self)
            if progress:
                for k in self.gmm_dict:
                    if self.gmm_dict[k] is not None:
                        self.gmm_dict[k].print_params()
            if epoch is not None and self.iterations % epoch == 0:
                self._reset()
            if verbose:
                # Standard mcsampler message, to monitor convergence
                print(" : {} {} {} {} {} ".format((self.iterations-1)*self.n, self.eff_samp, np.sqrt(2*np.max(self.cumulative_values)), np.sqrt(2*np.log(self.integral)), "-" ) )
        print('cumulative eval time: ', cumulative_eval_time)
        print('integrator iterations: ', self.iterations)
