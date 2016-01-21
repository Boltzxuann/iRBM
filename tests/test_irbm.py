import os
import sys

# Hack so you don't have to put the library containing this  script in the PYTHONPATH.
sys.path = [os.path.abspath(os.path.join(__file__, '..', '..'))] + sys.path

import numpy as np
import unittest
import shutil
import tempfile

import theano
import theano.tensor as T
from theano import config

from iRBM.models.rbm import RBM
from iRBM.models.orbm import oRBM
from iRBM.models.irbm import iRBM
from iRBM.misc.annealed_importance_sampling import compute_AIS

from theano.tensor.shared_randomstreams import RandomStreams
from theano.sandbox.rng_mrg import MRG_RandomStreams

from nose.tools import assert_equal, assert_true,assert_raises
from nose.plugins.skip import SkipTest

import numpy.testing as npt
from numpy.testing import (assert_array_equal,
                           assert_almost_equal,
                           assert_array_almost_equal,
                           assert_raises,
                           run_module_suite)

from iRBM.misc.utils import logsumexp
from iRBM.misc.utils import cartesian


class Test_iRBM(unittest.TestCase):
    def setUp(self):
        self.input_size = 4
        self.hidden_size = 7
        self.beta = 1.01
        self.batch_size = 100

        rng = np.random.RandomState(42)
        self.W = rng.randn(self.hidden_size, self.input_size).astype(config.floatX)
        self.b = rng.randn(self.hidden_size).astype(config.floatX)
        self.c = rng.randn(self.input_size).astype(config.floatX)

        self.model = iRBM(input_size=self.input_size,
                          hidden_size=self.hidden_size,
                          beta=self.beta)

        self.model.W.set_value(self.W)
        self.model.b.set_value(self.b)
        self.model.c.set_value(self.c)

    def test_beta(self):
        beta = 1.1
        model = iRBM(input_size=self.input_size,
                     #hidden_size=1000,
                     beta=beta)

        rng = np.random.RandomState(42)
        v1 = (rng.rand(1, self.input_size) > 0.5).astype(config.floatX)

        v = T.matrix('v')
        z = T.iscalar('z')
        F_vz = theano.function([v, z], model.F(v, z))

        # Suppose all parameters of the models have a value of 0 (i.e. l=0), then
        # as we add hidden units, $Z(v)=\sum_z exp(-F(v, z))$ should converge to
        geometric_ratio = T.exp((1.-model.beta) * T.nnet.softplus(0.)).eval()
        log_shifted_geometric_convergence = np.float32(np.log(geometric_ratio / (1. - geometric_ratio)))
        Zv_theorical_convergence = log_shifted_geometric_convergence

        # In fact, we can estimate the number of hidden units needed to be at $\epsilon$ of the convergence point.
        eps = 1e-7
        hidden_size = (np.log(eps)+np.log(1-geometric_ratio))/np.log(geometric_ratio)
        hidden_size = int(np.ceil(hidden_size))

        model.hidden_size = hidden_size
        model.W.set_value(np.zeros((model.hidden_size, model.input_size), dtype=theano.config.floatX))
        model.b.set_value(np.zeros((model.hidden_size,), dtype=theano.config.floatX))

        free_energies = []
        for z in range(1, model.hidden_size+1):
            free_energies.append(F_vz(v1, z))

        Z_v = logsumexp(-np.array(free_energies)).eval()
        print hidden_size, ':', Z_v, Zv_theorical_convergence, abs(Zv_theorical_convergence-Z_v)
        assert_almost_equal(Z_v, Zv_theorical_convergence, decimal=6)

    def test_free_energy(self):
        v = T.matrix('v')
        h = T.matrix('h')
        z = T.iscalar('z')
        logsumexp_E = theano.function([v, h, z], -logsumexp(-self.model.E(v, h, z)))
        F_vz = theano.function([v, z], self.model.F(v, z))

        rng = np.random.RandomState(42)
        v1 = (rng.rand(1, self.input_size) > 0.5).astype(config.floatX)
        H = cartesian([(0, 1)] * self.hidden_size, dtype=config.floatX)

        # Check the free energy F(v, z) is correct.
        for z in range(1, self.hidden_size+1):
            h = np.array(H[::2**(self.hidden_size-z)])
            free_energy_vz = logsumexp_E(v1, h, z)

            assert_almost_equal(F_vz(v1, z), free_energy_vz, decimal=6)

        # We now check that free energy F(v) assumes an infinite number of hidden units.
        # To do so, we create another model that has an infinite (read a lot) number of hidden units with parameters set to 0.
        nb_hidden_units_to_add = 10000
        model = iRBM(input_size=self.model.input_size,
                     hidden_size=self.model.hidden_size + nb_hidden_units_to_add,
                     beta=self.model.beta.get_value())

        model.W.set_value(np.r_[self.model.W.get_value(), np.zeros((nb_hidden_units_to_add, model.input_size), dtype=theano.config.floatX)])
        model.b.set_value(np.r_[self.model.b.get_value(), np.zeros((nb_hidden_units_to_add,), dtype=theano.config.floatX)])
        model.c.set_value(self.model.c.get_value())

        v = T.matrix('v')
        z = T.iscalar('z')
        F_vz = theano.function([v, z], model.F(v, z))

        free_energies_vz = []
        for z in range(1, model.hidden_size+1):
            free_energies_vz.append(F_vz(v1, z))

        Fv = -logsumexp(-np.array(free_energies_vz)).eval()

        v = T.matrix('v')
        free_energy = theano.function([v], self.model.free_energy(v))
        assert_array_almost_equal(free_energy(v1), [Fv], decimal=5)  # decimal=5 needed for float32

        v2 = np.tile(v1, (self.batch_size, 1))
        assert_array_almost_equal(free_energy(v2), [Fv]*self.batch_size, decimal=5)  # decimal=5 needed for float32

    def test_sample_z_given_v(self):
        v = T.matrix('v')
        z = T.iscalar('z')

        v1 = np.random.rand(1, self.input_size).astype(config.floatX)

        # We simulate having an infinite number of hidden units by adding lot of hidden units with parameters set to 0.
        nb_hidden_units_to_add = 10000
        model = iRBM(input_size=self.model.input_size,
                     hidden_size=self.model.hidden_size + nb_hidden_units_to_add,
                     beta=self.model.beta.get_value())

        model.W.set_value(np.r_[self.model.W.get_value(), np.zeros((nb_hidden_units_to_add, model.input_size), dtype=theano.config.floatX)])
        model.b.set_value(np.r_[self.model.b.get_value(), np.zeros((nb_hidden_units_to_add,), dtype=theano.config.floatX)])
        model.c.set_value(self.model.c.get_value())

        v = T.matrix('v')
        z = T.iscalar('z')
        F_vz = theano.function([v, z], model.F(v, z))

        energies = []
        for z in range(1, model.hidden_size+1):
            energies.append(F_vz(v1, z))

        energies = np.array(energies).T

        neg_log_probs = energies - -logsumexp(-energies, axis=1).eval()
        probs = np.exp(-neg_log_probs)
        expected_icdf = np.cumsum(probs[:, ::-1], axis=1)[:, ::-1]
        expected_icdf = expected_icdf[:, :self.model.hidden_size]

        # Test inverse cdf
        v = T.matrix('v')
        icdf_z_given_v = theano.function([v], self.model.icdf_z_given_v(v))
        assert_array_almost_equal(icdf_z_given_v(v1), expected_icdf, decimal=5)  # decimal=5 needed for float32

        batch_size = 500000
        self.model.batch_size = batch_size
        sample_zmask_given_v = theano.function([v], self.model.sample_zmask_given_v(v))
        v2 = np.tile(v1, (self.model.batch_size, 1))

        #theano.printing.pydotprint(sample_zmask_given_v)

        z_mask = sample_zmask_given_v(v2)
        # First hidden units should always be considered i.e. z_mask[:, 0] == 1
        assert_equal(np.sum(z_mask[:, 0] == 0, axis=0), 0)

        # Test that sampled masks are as expected i.e. equal expected_icdf
        freq_per_z = np.sum(z_mask, axis=0) / self.model.batch_size
        assert_array_almost_equal(freq_per_z, expected_icdf[0], decimal=3, err_msg="Tested using MC sampling, rerun it to be certain that is an error or increase 'batch_size'.")

    def test_compute_lnZ(self):
        v = T.matrix('v')
        z = T.iscalar('z')

        V = cartesian([(0, 1)] * self.input_size, dtype=config.floatX)
        #H = cartesian([(0, 1)] * self.hidden_size, dtype=config.floatX)

        # We simulate having an infinite number of hidden units by adding lot of hidden units with parameters set to 0.
        nb_hidden_units_to_add = 10000
        model = iRBM(input_size=self.model.input_size,
                     hidden_size=self.model.hidden_size + nb_hidden_units_to_add,
                     beta=self.model.beta.get_value())

        model.W.set_value(np.r_[self.model.W.get_value(), np.zeros((nb_hidden_units_to_add, model.input_size), dtype=theano.config.floatX)])
        model.b.set_value(np.r_[self.model.b.get_value(), np.zeros((nb_hidden_units_to_add,), dtype=theano.config.floatX)])
        model.c.set_value(self.model.c.get_value())

        v = T.matrix('v')
        z = T.iscalar('z')
        F_vz = theano.function([v, z], model.F(v, z))

        energies = []
        for z in range(1, model.hidden_size+1):
            energies.append(F_vz(V, z))

        lnZ = logsumexp(-np.array(energies)).eval()

        lnZ_using_free_energy = theano.function([v], logsumexp(-self.model.free_energy(v)))
        assert_almost_equal(lnZ_using_free_energy(V), lnZ, decimal=5)  # decimal=5 needed for float32

    def test_base_rate(self):
        # All binary combinaisons for V and H_z
        V = cartesian([(0, 1)] * self.input_size, dtype=config.floatX)
        #H = cartesian([(0, 1)] * self.hidden_size, dtype=config.floatX)

        base_rates = []
        # Add the uniform base rate, i.e. all parameters of the model are set to 0.
        base_rates.append(self.model.get_base_rate())
        # Add the base rate where visible biases are the ones from the model.
        base_rates.append(self.model.get_base_rate('c'))
        # Add the base rate where hidden biases are the ones from the model.
        # base_rates.append(self.model.get_base_rate('b'))  # Not implemented

        for base_rate, anneable_params in base_rates:
            print base_rate
            base_rate_lnZ = base_rate.compute_lnZ().eval().astype(config.floatX)

            # We simulate having an infinite number of hidden units by adding lot of hidden units with parameters set to 0.
            nb_hidden_units_to_add = 10000
            model = iRBM(input_size=base_rate.input_size,
                         hidden_size=base_rate.hidden_size + nb_hidden_units_to_add,
                         beta=base_rate.beta.get_value())

            model.W = T.join(0, base_rate.W, np.zeros((nb_hidden_units_to_add, model.input_size), dtype=theano.config.floatX))
            model.b = T.join(0, base_rate.b, np.zeros((nb_hidden_units_to_add,), dtype=theano.config.floatX))
            model.c = base_rate.c

            v = T.matrix('v')
            z = T.iscalar('z')
            F_vz = theano.function([v, z], model.F(v, z))

            energies = []
            for z in range(1, model.hidden_size+1):
                energies.append(F_vz(V, z))

            brute_force_lnZ = logsumexp(-np.array(energies)).eval()
            assert_almost_equal(brute_force_lnZ.astype(config.floatX), base_rate_lnZ, decimal=5)

            theano_lnZ = logsumexp(-base_rate.free_energy(V), axis=0).eval()
            assert_almost_equal(theano_lnZ.astype(config.floatX), base_rate_lnZ, decimal=6)

    def test_gradients_auto_vs_manual(self):
        rng = np.random.RandomState(42)

        batch_size = 5
        input_size = 10

        model = iRBM(input_size=input_size,
                     hidden_size=32,
                     beta=1.01,
                     CDk=1,
                     rng=np.random.RandomState(42))

        W = rng.rand(model.hidden_size, model.input_size).astype(theano.config.floatX)
        model.W = theano.shared(value=W.astype(theano.config.floatX), name='W', borrow=True)

        b = rng.rand(model.hidden_size).astype(theano.config.floatX)
        model.b = theano.shared(value=b.astype(theano.config.floatX), name='b', borrow=True)

        c = rng.rand(model.input_size).astype(theano.config.floatX)
        model.c = theano.shared(value=c.astype(theano.config.floatX), name='c', borrow=True)

        params = [model.W, model.b, model.c]
        chain_start = T.matrix('start')
        chain_end = T.matrix('end')

        chain_start_value = (rng.rand(batch_size, input_size) > 0.5).astype(theano.config.floatX)
        chain_end_value = (rng.rand(batch_size, input_size) > 0.5).astype(theano.config.floatX)
        chain_start.tag.test_value = chain_start_value
        chain_end.tag.test_value = chain_end_value

        ### Computing gradients using automatic differentation ###
        cost = T.mean(model.free_energy(chain_start)) - T.mean(model.free_energy(chain_end))
        gparams_auto = T.grad(cost, params, consider_constant=[chain_end])

        ### Computing gradients manually ###
        h = RBM.sample_h_given_v(model, chain_start, return_probs=True)
        _h = RBM.sample_h_given_v(model, chain_end, return_probs=True)
        icdf = model.icdf_z_given_v(chain_start)
        _icdf = model.icdf_z_given_v(chain_end)

        if model.penalty == "softplus_bi":
            penalty = model.beta * T.nnet.sigmoid(model.b)
        elif self.penalty == "softplus0":
            penalty = model.beta * T.nnet.sigmoid(0)

        grad_W = (T.dot(chain_end.T, _h*_icdf) - T.dot(chain_start.T, h*icdf)).T / batch_size
        grad_b = T.mean((_h-penalty)*_icdf - (h-penalty)*icdf, axis=0)
        grad_c = T.mean(chain_end - chain_start, axis=0)

        gparams_manual = [grad_W, grad_b, grad_c]
        grad_W.name, grad_b.name, grad_c.name = "grad_W", "grad_b", "grad_c"

        for gparam_auto, gparam_manual in zip(gparams_auto, gparams_manual):
            param1 = gparam_auto.eval({chain_start: chain_start_value, chain_end: chain_end_value})
            param2 = gparam_manual.eval({chain_start: chain_start_value, chain_end: chain_end_value})
            assert_array_almost_equal(param1, param2, err_msg=gparam_manual.name, decimal=5)  # decimal=5 needed for float32


class TestAIS_iRBM(unittest.TestCase):
    def setUp(self):
        self.nb_samples = 1000
        self.input_size = 10
        self.hidden_size = 14
        self.beta = 1.01

        rng = np.random.RandomState(42)
        self.W = rng.rand(self.hidden_size, self.input_size).astype(config.floatX)
        self.b = rng.rand(self.hidden_size).astype(config.floatX)
        self.c = rng.rand(self.input_size).astype(config.floatX)

        self.betas = np.r_[np.linspace(0, 0.5, num=500), np.linspace(0.5, 0.9, num=4000), np.linspace(0.9, 1, num=10000)]

    def test_verify_AIS(self):
        model = iRBM(input_size=self.input_size,
                     hidden_size=self.hidden_size,
                     beta=self.beta)

        model.W.set_value(self.W)
        model.b.set_value(self.b)
        model.c.set_value(self.c)

        # Brute force
        print "Computing lnZ using brute force (i.e. summing the free energy of all posible $v$)..."
        V = theano.shared(value=cartesian([(0, 1)] * self.input_size, dtype=config.floatX))
        brute_force_lnZ = logsumexp(-model.free_energy(V), 0)
        f_brute_force_lnZ = theano.function([], brute_force_lnZ)

        params_bak = [param.get_value() for param in model.parameters]

        print "Approximating lnZ using AIS..."
        import time
        start = time.time()

        try:
            ais_working_dir = tempfile.mkdtemp()
            result = compute_AIS(model, M=self.nb_samples, betas=self.betas, seed=1234, ais_working_dir=ais_working_dir, force=True)
            logcummean_Z, logcumstd_Z_down, logcumstd_Z_up = result['logcummean_Z'], result['logcumstd_Z_down'], result['logcumstd_Z_up']
            std_lnZ = result['std_lnZ']

            print "{0} sec".format(time.time() - start)

            import pylab as plt
            plt.gca().set_xmargin(0.1)
            plt.errorbar(range(1, self.nb_samples+1), logcummean_Z, yerr=[std_lnZ, std_lnZ], fmt='or')
            plt.errorbar(range(1, self.nb_samples+1), logcummean_Z, yerr=[logcumstd_Z_down, logcumstd_Z_up], fmt='ob')
            plt.plot([1, self.nb_samples], [f_brute_force_lnZ()]*2, '--g')
            plt.ticklabel_format(useOffset=False, axis='y')
            plt.show()
            AIS_logZ = logcummean_Z[-1]

            assert_array_equal(params_bak[0], model.W.get_value())
            assert_array_equal(params_bak[1], model.b.get_value())
            assert_array_equal(params_bak[2], model.c.get_value())

            print np.abs(AIS_logZ - f_brute_force_lnZ())
            assert_almost_equal(AIS_logZ, f_brute_force_lnZ(), decimal=2)
        finally:
            shutil.rmtree(ais_working_dir)


if __name__ == '__main__':
    run_module_suite()
