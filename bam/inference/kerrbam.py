import os
import sys
import numpy as np
import ehtim as eh
import matplotlib.pyplot as plt
import random
from bam.inference.model_helpers import Gpercsq, M87_ra, M87_dec, M87_mass, M87_dist, M87_inc, isiterable, get_rho_varphi_from_FOV_npix, rescale_veclist, rice
from bam.inference.data_helpers import make_log_closure_amplitude, amp_add_syserr, vis_add_syserr, logcamp_add_syserr, cphase_add_syserr, get_cphase_uvpairs, cphase_uvdists, get_logcamp_uvpairs, logcamp_uvdists, get_camp_amp_sigma, get_cphase_vis_sigma, var_sys, get_minimal_logcamps, get_minimal_cphases
from numpy import arctan2, sin, cos, exp, log, clip, sqrt,sign
import dynesty
from dynesty import plotting as dyplot
from dynesty import utils as dyfunc
from scipy.optimize import dual_annealing
from scipy.special import ive
import time
from ehtim.plotting.summary_plots import imgsum
from ehtim.calibrating.self_cal import self_cal
# from bam.inference.schwarzschildexact import getscreencoords, getwindangle, getpsin, getalphan
# from bam.inference.gradients import LogLikeGrad, LogLikeWithGrad, exact_vis_loglike
# from ehtim.observing.pulses import deltaPulse2D
import bam
from tqdm import tqdm
import dill as pkl

# pkl.settings['recurse']=True

def get_uniform_transform(lower, upper):
    return lambda x: (upper-lower)*x + lower


NOISE_DEFAULT_DICT = {'f':0,'e':0,'var_a':0,'var_b':0,'var_c':0,'var_u0':4e9}

class KerrBam:
    '''The Bam class is a collection of accretion flow and black hole parameters.
    jfunc: a callable that takes (r, phi, jargs)
    if Bam is in modeling mode, jfunc should use pm functions
    '''
    #class contains knowledge of a grid in Boyer-Lindquist coordinates, priors on each pixel, and the machinery to fit them
    def __init__(self, fov, npix, jfunc, jarg_names, jargs, MoDuas, a, inc, zbl,  xuas = 0., yuas = 0., PA=0.,  nmax=0, beta=0., chi=0., eta = None, iota=np.pi/2, spec=1., alpha_zeta = None, h = 1, polfrac=0.7, dEVPA=0, f=0., e=0., var_a = 0, var_b = 0, var_c = 0, var_u0=4e9, polflux=True, source='', periodic=False, adap_fac =1, axisymmetric = True, stationary = True, optical_depth='thin',compute_P=True,compute_V=False,interp_order=1, use_jax=False, rice_amps=False, times=np.array([0]), r_o=np.inf):
        if use_jax:
            print("Using jax is not recommended for an adaptive model.")
            self.rtfunc = bam.inference.jax_kerrexact.kerr_exact_sep_lp
        else:
            self.rtfunc = bam.inference.kerrexact.kerr_exact_sep_lp   
        self.rice_amps = rice_amps      
        self.interp_order = interp_order
        self.compute_P = compute_P
        self.compute_V = compute_V
        self.optical_depth = optical_depth
        self.axisymmetric=axisymmetric
        self.stationary=stationary
        self.times = times
        self.periodic=periodic
        self.source = source
        self.polflux = polflux
        self.fov = fov
        self.fov_uas = fov/eh.RADPERUAS
        self.npix = npix
        self.recent_loglike = None
        self.recent_sampler = None
        self.recent_results = None
        # self.MAP_values = None
        self.jfunc = jfunc
        self.jarg_names = jarg_names
        self.jargs = jargs
        # self.M = M
        # self.D = D
        self.MoDuas = MoDuas
        self.a = a
        self.inc = inc
        self.xuas = xuas
        self.yuas = yuas
        self.PA = PA
        self.beta = beta
        self.chi = chi
        self.eta = eta
        self.iota = iota
        self.spec = spec
        self.alpha_zeta = alpha_zeta
        self.h = h
        self.polfrac = polfrac
        self.dEVPA = dEVPA
        self.f = f
        self.e = e
        self.var_a = var_a
        self.var_b = var_b
        self.var_c = var_c
        self.var_u0 = var_u0
        self.nmax = nmax
        self.zbl = zbl
        self.r_o = r_o
        if self.nmax == 0 and adap_fac != 1:
            print ("You are trying to use adaptive ray tracing for non-existant sub-images. adap_fac is being forced to 1.")
            self.adap_fac = 1
        else:
            self.adap_fac = adap_fac
        if adap_fac != 1:
            print("Using adaptive ray-tracing! npix is interpreted as n=0 resolution only.")
        self.rho_c = np.sqrt(27)
        # self.Mscale = Mscale
        self.rho_uas, self.varphivec = get_rho_varphi_from_FOV_npix(self.fov_uas, self.npix, adap_fac=self.adap_fac, nmax=nmax)

        # #while we're at it, get x and y
        # self.imxvec = -self.rho_uas*np.cos(self.varphivec)
       
        # self.imyvec = self.rho_uas*np.sin(self.varphivec)
        if any([isiterable(i) for i in [MoDuas, a, inc, zbl, xuas, yuas, PA, f, beta, chi, iota, e, spec, alpha_zeta, h, polfrac, dEVPA]+jargs]):
            mode = 'model'
        else:
            mode = 'fixed' 
        self.mode = mode
        self.noise_param_names = ['f','e','var_a','var_b','var_c','var_u0']
        self.all_params = [MoDuas, a, inc, zbl, xuas, yuas, PA, beta, chi,eta, iota, spec, alpha_zeta, h, polfrac, dEVPA, f, e, var_a, var_b, var_c, var_u0]+jargs
        self.all_names = ['MoDuas','a', 'inc','zbl','xuas','yuas','PA','beta','chi','eta','iota','spec','alpha_zeta','h','polfrac','dEVPA','f','e','var_a','var_b','var_c','var_u0']+jarg_names
        self.imparam_names = [i for i in self.all_names if i not in self.noise_param_names+jarg_names]
        self.imparam_names = self.imparam_names + ['jargs']
        self.all_param_dict = dict()
        for i in range(len(self.all_names)):
            self.all_param_dict[self.all_names[i]] = self.all_params[i]

        self.modeled_indices = [i for i in range(len(self.all_params)) if isiterable(self.all_params[i])]
        self.modeled_names = [self.all_names[i] for i in self.modeled_indices]
        self.error_modeling = np.any(np.array([i in self.modeled_names for i in ['f','e','var_a','var_b','var_c','var_u0']]))# or self.f != 0. or self.e != 0.
        self.adding_syserr = np.any(np.array([self.all_param_dict[i] != 0 for i in ['f','e','var_a','var_b','var_c']]))
        self.modeled_params = [i for i in self.all_params if isiterable(i)]
        self.modeled_param_dict = dict()
        for i in range(len(self.modeled_names)):
            self.modeled_param_dict[self.modeled_names[i]]=self.modeled_params[i]

        if not self.stationary:
            if self.mode =='model':
                print("Can't yet fit data with a slowlight model. Turning off slowlight")
                self.stationary=True
            if self.axisymmetric:
                print("Non-stationary flow requires non-axisymmetry for now.")
                self.axisymmetric = False
            if r_o == np.inf:
                print("Cannot use infinite camera distance with non-stationary model! Defaulting to 1e4 M.")
                self.r_o = 1.e4
        if self.periodic:
            print("Periodic priors are not currently functional. Reverting to non-periodic.")
            self.periodic = False
        # self.periodic_names = []
        # self.periodic_indices=[]
        # if self.periodic:
        #     for i in ['PA','chi']:
        #         if i in self.modeled_names:
        #             bounds = self.modeled_params[self.modeled_names.index(i)]
        #             if np.isclose(np.exp(1j*bounds[0]),np.exp(1j*bounds[1]),rtol=1e-12):
        #                 print("Found periodic prior on "+str(i))
        #                 self.periodic_names.append(i)
        #                 self.periodic_indices.append(self.modeled_names.index(i))
        self.model_dim = len(self.modeled_names)

        if self.mode == 'fixed':
            self.imparams = [self.MoDuas, self.a, self.inc, self.zbl, self.xuas, self.yuas, self.PA, self.beta, self.chi, self.eta, self.iota, self.spec, self.alpha_zeta, self.h, self.polfrac, self.dEVPA, self.jargs]
            # self.rhovec = self.rho_uas / self.MoDuas
            # self.rhovec = D/(M*Mscale*Gpercsq*self.rho_uas)
            # if self.exacttype=='interp' and all([not(self.all_interps[i] is None) for i in range(len(self.all_interps))]):
            print("Fixed Bam: precomputing all subimages.")
            if self.stationary:
                self.ivecs, self.qvecs, self.uvecs, self.vvecs = self.compute_image(self.imparams)
            else:
                self.frames = self.compute_image(self.imparams)
        self.modelim = None
        print("Finished building KerrBam! in "+ self.mode +" mode!")#" with exacttype " +self.exacttype)


    def test(self, i, out):
        plt.close('all')
        if len(i) == self.npix**2:
            i = i.reshape((self.npix, self.npix))
        plt.imshow(i)
        plt.colorbar()
        plt.savefig(out+'.png',bbox_inches='tight')
        plt.close('all')
        # plt.show()

    def get_primitives(self):
        """
        In fixed mode, return the output of kerr_exact associated with the current image grid.
        """
        if self.mode != 'fixed':
            print("Can't directly evaluate kerr_exact in model mode!")
            return

        MoDuas, a, inc, zbl, xuas, yuas, PA, beta, chi, eta, iota, spec, alpha_zeta, h, polfrac, dEVPA, jargs = self.imparams

        
        #convert rho_uas to gravitational units
        # rhovec = self.rho_uas/MoDuas
        print("Warning: primitives have changed recently.")
        print("Returning: rvecs, phivecs, tvecs, ivecs, qvecs, uvecs, vvecs, redshifts, lps.")
        return self.rtfunc(self.rho_uas, MoDuas, self.varphivec, inc, a, self.nmax, beta, chi, eta, iota, spec, alpha_zeta, adap_fac = self.adap_fac, axisymmetric=self.axisymmetric, stationary=self.stationary, compute_V = self.compute_V, r_o = self.r_o)        



    def compute_image(self, imparams):
        """
        Given a list of values of modeled parameters in imparams,
        compute the resulting i, q, u, v
        """
        # print(imparams)
        MoDuas, a, inc, zbl, xuas, yuas, PA, beta, chi, eta, iota, spec, alpha_zeta, h, polfrac, dEVPA, jargs = imparams

        
        #convert rho_uas to gravitational units
        rvecs, phivecs, tvecs, ivecs, qvecs, uvecs, vvecs, redshifts, lps = self.rtfunc(self.rho_uas, MoDuas, self.varphivec, inc, a, self.nmax, beta, chi, eta, iota, spec, alpha_zeta, adap_fac = self.adap_fac, axisymmetric=self.axisymmetric, stationary=self.stationary, compute_V = self.compute_V, r_o=self.r_o)
        if not(self.compute_P) or not(self.compute_V):
            zvecs = [np.zeros_like(rvecs[n]) for n in range(self.nmax+1)]
        if self.optical_depth == 'varying' or self.optical_depth == 'thick':
            rvecs = rescale_veclist(rvecs,order=self.interp_order,anti_aliasing=False)
            if not self.axisymmetric:
                phivecs = rescale_veclist(phivecs,order=self.interp_order,anti_aliasing=False)
            if not self.stationary:
                tvecs = rescale_veclist(tvecs,order=self.interp_order,anti_aliasing=False)
            redshifts = rescale_veclist(redshifts,order=self.interp_order,anti_aliasing=False)
            lps = rescale_veclist(lps,order=self.interp_order,anti_aliasing=False)
            ivecs = rescale_veclist(ivecs,order=self.interp_order,anti_aliasing=False)
            if self.compute_P:
                qvecs = rescale_veclist(qvecs,order=self.interp_order,anti_aliasing=False)
                uvecs = rescale_veclist(uvecs,order=self.interp_order,anti_aliasing=False)
            if self.compute_V:
                vvecs = rescale_veclist(vvecs,order=self.interp_order,anti_aliasing=False)
        if self.stationary:

            for n in reversed(range(self.nmax+1)):
                if self.axisymmetric:
                    jfunc_vals = self.jfunc(rvecs[n], jargs) 
                else:
                    jfunc_vals = self.jfunc(rvecs[n],phivecs[n],jargs)

                profile = jfunc_vals*redshifts[n]**(3+spec)

                if self.optical_depth == 'thin':
                    profile = profile * lps[n]
                elif self.optical_depth == 'varying':
                    tau = h*lps[n]
                    exptau = np.exp(-tau)
                    profile = profile * (1-exptau)
                    if n < self.nmax:
                        ivecs[n+1] *= exptau
                        if self.compute_P:
                            qvecs[n+1] *= exptau
                            uvecs[n+1] *= exptau
                        if self.compute_V:
                            vvecs[n+1] *= exptau
                elif self.optical_depth == 'thick':
                    #this is the optically thick case, where h is a constant
                    pass
                else:
                    print("Unrecognized optical depth prescription! Defaulting to optically thick.")
                    
                if self.polflux:
                    ivecs[n]*=profile
                else:
                    ivecs[n] = profile
                    qvecs[n] = zvecs[n]
                    uvecs[n] = zvecs[n]
                    vvecs[n] = zvecs[n]
                if self.compute_P:
                    qvecs[n]*=profile
                    uvecs[n]*=profile
                if self.compute_V:
                    vvecs[n]*=profile
            if self.optical_depth == 'thin':
                ivecs = rescale_veclist(ivecs,order=self.interp_order,anti_aliasing=False)
                qvecs = rescale_veclist(qvecs,order=self.interp_order,anti_aliasing=False)
                uvecs = rescale_veclist(uvecs,order=self.interp_order,anti_aliasing=False)
                vvecs = rescale_veclist(vvecs,order=self.interp_order,anti_aliasing=False)
            tf = np.sum(ivecs)
            ivecs = [ivec*zbl/tf for ivec in ivecs]
            if self.compute_P:
                qvecs = [qvec*zbl/tf*polfrac for qvec in qvecs]
                uvecs = [uvec*zbl/tf*polfrac for uvec in uvecs]
                if not np.isclose(0.,dEVPA):
                    pvecs = [(qvecs[i]+1j*uvecs[i])*np.exp(2j*dEVPA) for i in range(len(qvecs))]
                    qvecs = [np.real(pvec) for pvec in pvecs]
                    uvecs = [np.imag(pvec) for pvec in pvecs]
            if self.compute_V:
                vvecs = [vvec*zbl/tf*polfrac for vvec in vvecs]
            return ivecs, qvecs, uvecs, vvecs 
        else:
            out = []
            for time in self.times:
                for n in reversed(range(self.nmax+1)):
                    jfunc_vals = self.jfunc(rvecs[n],phivecs[n],tvecs[n]+time,jargs)
                    profile = jfunc_vals*redshifts[n]**(3+spec)

                    if self.optical_depth == 'thin':
                        profile = profile * lps[n]
                    elif self.optical_depth == 'varying':
                        tau = h*lps[n]
                        exptau = np.exp(-tau)
                        profile = profile * (1-exptau)
                        if n < self.nmax:
                            ivecs[n+1] *= exptau
                            if self.compute_P:
                                qvecs[n+1] *= exptau
                                uvecs[n+1] *= exptau
                            if self.compute_V:
                                vvecs[n+1] *= exptau
                    elif self.optical_depth == 'thick':
                        #this is the optically thick case, where h is a constant
                        pass
                    else:
                        print("Unrecognized optical depth prescription! Defaulting to optically thick.")
                        
                    if self.polflux:
                        ivecs[n]*=profile
                    else:
                        ivecs[n] = profile
                        qvecs[n] = zvecs[n]
                        uvecs[n] = zvecs[n]
                        vvecs[n] = zvecs[n]
                    if self.compute_P:
                        qvecs[n]*=profile
                        uvecs[n]*=profile
                    if self.compute_V:
                        vvecs[n]*=profile
                if self.optical_depth == 'thin':
                    ivecs = rescale_veclist(ivecs,order=self.interp_order,anti_aliasing=False)
                    qvecs = rescale_veclist(qvecs,order=self.interp_order,anti_aliasing=False)
                    uvecs = rescale_veclist(uvecs,order=self.interp_order,anti_aliasing=False)
                    vvecs = rescale_veclist(vvecs,order=self.interp_order,anti_aliasing=False)
                tf = np.sum(ivecs)
                ivecs = [ivec*zbl/tf for ivec in ivecs]
                if self.compute_P:
                    qvecs = [qvec*zbl/tf*polfrac for qvec in qvecs]
                    uvecs = [uvec*zbl/tf*polfrac for uvec in uvecs]
                    if not np.isclose(0.,dEVPA):
                        pvecs = [(qvecs[i]+1j*uvecs[i])*np.exp(2j*dEVPA) for i in range(len(qvecs))]
                        qvecs = [np.real(pvec) for pvec in pvecs]
                        uvecs = [np.imag(pvec) for pvec in pvecs]
                if self.compute_V:
                    vvecs = [vvec*zbl/tf*polfrac for vvec in vvecs]
                out.append([ivecs, qvecs, uvecs, vvecs])
            return out



    def observe_same(self, obs, ampcal=True,phasecal=True,add_th_noise=True,seed=None):
        if seed is None:
            seed = random.randrange(sys.maxsize)
        if self.mode=='model':
            print("Can't observe_same in model mode!")
            return
        im = self.make_image(ra=obs.ra, dec=obs.dec, rf=obs.rf, mjd = obs.mjd, source=obs.source)
        return im.observe_same(obs, ampcal=ampcal,phasecal=phasecal, add_th_noise=add_th_noise, seed=seed)

    def modelim_ivis(self, uv, ttype='nfft'):
        return self.modelim.sample_uv(uv,ttype=ttype)[0]

    def modelim_allvis(self, uv, ttype='nfft'):
        return self.modelim.sample_uv(uv,ttype=ttype)

    def modelim_logcamp(self, uv1, uv2, uv3, uv4, ttype='nfft'):
        vis12 = self.modelim_ivis(uv1,ttype=ttype)
        vis34 = self.modelim_ivis(uv2,ttype=ttype)
        vis23 = self.modelim_ivis(uv3,ttype=ttype)
        vis14 = self.modelim_ivis(uv4,ttype=ttype)
        amp12 = np.abs(vis12)
        amp34 = np.abs(vis34)
        amp23 = np.abs(vis23)
        amp14 = np.abs(vis14)
        logcamp_model = np.log(amp12)+np.log(amp34)-np.log(amp23)-np.log(amp14)
        return logcamp_model

    def modelim_cphase(self, uv1, uv2, uv3, ttype='nfft'):
        vis12 = self.modelim_ivis(uv1,ttype=ttype)
        vis23 = self.modelim_ivis(uv2,ttype=ttype)
        vis31 = self.modelim_ivis(uv3,ttype=ttype)
        phase12 = np.angle(vis12)
        phase23 = np.angle(vis23)
        phase31 = np.angle(vis31)
        cphase_model = phase12+phase23+phase31
        return cphase_model

    def pad_imparams(self, imparams, noiseparams):
        infi = imparams[:-1] + noiseparams+imparams[-1:]
        return infi

    def build_eval(self, indexable_fitparams):
        infi = indexable_fitparams
        to_eval = dict()
        for name in self.all_names:
            if not(name in self.modeled_names):
                to_eval[name] = self.all_param_dict[name]
            else:
                to_eval[name] = infi[self.modeled_names.index(name)]
        jargs = []
        for jn in self.jarg_names:
            jargs.append(to_eval[jn])
            del to_eval[jn]
        to_eval['jargs'] = jargs
        return to_eval

    def loglike_of_Bam(self, fbam):
        """
        Given a fixed-mode Bam object, compute the log likelihood of its parameters given
        the current model Bam's fit parameters.
        """
        params = []
        for name in self.modeled_names:
            params.append(fbam.all_param_dict[name])
        return self.recent_loglike(params)
    
    def build_nxcorr(self, im):
        """
        Given an observation and a list of data product names, 
        return a likelihood function that accounts for each contribution. 
        """
        if not self.stationary:
            print("Can't use NxCorr in time-dependent mode!")
            return
        def nxcorr(params):
            to_eval = self.build_eval(params)

            imparams = [to_eval[ipn] for ipn in self.imparam_names]
            ivecs, qvecs, uvecs, vvecs = self.compute_image(imparams)
            out = 0.
            ivec = np.sum(ivecs,axis=0)
            if self.compute_P:
                qvec = np.sum(qvecs,axis=0)
                uvec = np.sum(uvecs,axis=0)
            else:
                qvec = np.zeros_like(ivec)
                uvec = np.zeros_like(ivec)
            if self.compute_V:
                vvec = np.sum(vvecs,axis=0)
            else:
                vvec = np.zeros_like(ivec)
            self.modelim.ivec = ivec
            self.modelim.qvec = qvec
            self.modelim.uvec = uvec
            self.modelim_vvec = vvec
            # self.modelim.pa = to_eval['PA']
            
            return im.compare_images(self.modelim.rotate(to_eval['PA']),metric='nxcorr')[0][0]

        print("Built nxcorr function!")
        self.nxcorr = nxcorr
        return nxcorr

    def build_nrmse(self, im):
        """
        Given an observation and a list of data product names, 
        return a likelihood function that accounts for each contribution. 
        """

        if not self.stationary:
            print("Can't use NRMSE in time-dependent mode!")
            return

        def nrmse(params):
            to_eval = self.build_eval(params)

            imparams = [to_eval[ipn] for ipn in self.imparam_names]
            ivecs, qvecs, uvecs, vvecs = self.compute_image(imparams)
            out = 0.
            ivec = np.sum(ivecs,axis=0)
            qvec = np.sum(qvecs,axis=0)
            uvec = np.sum(uvecs,axis=0)
            vvec = np.sum(vvecs,axis=0)

            self.modelim.ivec = ivec
            self.modelim.qvec = qvec
            self.modelim.uvec = uvec
            self.modelim_vvec = vvec
            # self.modelim.pa = to_eval['PA']
            
            return im.compare_images(self.modelim.rotate(to_eval['PA']),metric='nrmse')[0][0]

        print("Built nxcorr function!")
        self.nrmse = nrmse
        return nrmse

    def build_likelihood(self, obs, data_types=['vis'], ttype='nfft', debias = True, compute_minimal=True,load_recent=False):
        """
        Given an observation and a list of data product names, 
        return a likelihood function that accounts for each contribution. 
        """

        u = obs.data['u']
        v = obs.data['v']
        uvdists = np.sqrt(u**2+v**2)
        
        if 'vis' in data_types:
            vis = obs.data['vis']
            sigma = obs.data['sigma']
            amp = obs.unpack('amp',debias=debias)['amp']
            if not(self.error_modeling) and self.adding_syserr:
                _, sigma = amp_add_syserr(amp, sigma, fractional=self.f, additive = self.e, var_a = self.var_a, var_b=self.var_b, var_c=self.var_c, var_u0=self.var_u0, u = uvdists)
            
            # u = obs.data['u']
            # v = obs.data['v']
            visuv = np.vstack([u,v]).T
            Nvis = len(vis)
            print("Building vis likelihood!")
        if 'qvis' in data_types:
            qvis = obs.data['qvis']
            qsigma = obs.data['qsigma']
            qamp = np.abs(qvis)
            # u = obs.data['u']
            # v = obs.data['v']
            visuv = np.vstack([u,v]).T
            Nqvis = len(qvis)
        if 'uvis' in data_types:
            uvis = obs.data['uvis']
            usigma = obs.data['usigma']
            uamp = np.abs(uvis)
            # u = obs.data['u']
            # v = obs.data['v']
            visuv = np.vstack([u,v]).T
            Nuvis = len(uvis)
        if 'vvis' in data_types:
            vvis = obs.data['vvis']
            vsigma = obs.data['vsigma']
            vamp = np.abs(vvis)
            # u = obs.data['u']
            # v = obs.data['v']
            visuv = np.vstack([u,v]).T
            Nvvis = len(vvis)
        if 'mvis' in data_types:
            vis = obs.data['vis']
            qvis = obs.data['qvis']
            uvis = obs.data['uvis']
            pvis = qvis+1j*uvis
            sigma = obs.data['sigma']
            amp = obs.unpack('amp', debias=debias)['amp']
            if not(self.error_modeling) and self.adding_syserr:
                _, sigma = amp_add_syserr(amp, sigma, fractional=self.f, additive = self.e, var_a = self.var_a, var_b=self.var_b, var_c=self.var_c, var_u0=self.var_u0, u = uvdists)
            mvis = pvis/vis
            msigma = sigma * np.sqrt(2/np.abs(vis)**2 + np.abs(pvis)**2 / np.abs(vis)**4)
            mvis_ln_norm = -2*np.sum(np.log((2.0*np.pi)**0.5*msigma))
            # u = obs.data['u']
            # v = obs.data['v']
            visuv = np.vstack([u,v]).T
            Nmvis = len(mvis)
        if 'amp' in data_types:
            sigma = obs.data['sigma']
            amp = obs.unpack('amp', debias=debias)['amp']
            
            ampuv = np.vstack([u,v]).T
            Namp = len(amp)
            print("Building amp likelihood!")
        if 'logcamp' in data_types:
            print("Building logcamp likelihood!")
            if compute_minimal:
                if load_recent:
                    logcamp_data = np.genfromtxt('logcamps.txt',dtype=None,names=['time','t1','t2','t3','t4','u1','u2','u3','u4','v1','v2','v3','v4','camp','sigmaca'])
                    logcamp_design_mat = np.loadtxt('logcamp_design_matrix.txt')
                    logcamp_uvpairs = np.loadtxt('logcamp_uvpairs.txt')
                else:
                    logcamp_data, logcamp_design_mat, logcamp_uvpairs = get_minimal_logcamps(obs,debias=debias)
            else:
                logcamp_data = obs.c_amplitudes(ctype='logcamp', debias=debias)
            logcamp = logcamp_data['camp']
            logcamp_sigma = logcamp_data['sigmaca']
            campuv1, campuv2, campuv3, campuv4 = get_logcamp_uvpairs(logcamp_data)
            if self.error_modeling or self.adding_syserr:
                print("Back-fetching quadrangle ampltudes and sigmas.")
                n1amp, n2amp, d1amp, d2amp, n1err, n2err, d1err, d2err = get_camp_amp_sigma(obs, logcamp_data)
                campd1, campd2, campd3, campd4 = logcamp_uvdists(logcamp_data)
                print("Done!")
            if not(self.error_modeling):
                if self.adding_syserr:
                    _, logcamp_sigma = logcamp_add_syserr(n1amp, n2amp, d1amp, d2amp, n1err, n2err, d1err, d2err, campd1, campd2, campd3, campd4, fractional=self.f, additive = self.e, var_a = self.var_a, var_b=self.var_b, var_c=self.var_c, var_u0=self.var_u0, debias=debias)
                logcamp_ln_norm = -np.sum(np.log((2.0*np.pi)**0.5 * logcamp_sigma))
                
            Ncamp = len(logcamp)
        if 'cphase' in data_types:
            print("Building cphase likelihood!")
            if compute_minimal:
                if load_recent:
                    cphase_data = np.genfromtxt('cphases.txt',dtype=None,names=['time','t1','t2','t3','u1','u2','u3','v1','v2','v3','cphase','sigmacp'])
                    cphase_design_mat = np.loadtxt('cphase_design_matrix.txt')
                    cphase_uvpairs = np.loadtxt('cphase_uvpairs.txt')
                else:
                    cphase_data, cphase_design_mat, cphase_uvpairs = get_minimal_cphases(obs)
            else:
                cphase_data = obs.c_phases(ang_unit='rad')
            cphaseuv1, cphaseuv2, cphaseuv3 = get_cphase_uvpairs(cphase_data)
            cphase = cphase_data['cphase']
            cphase_sigma = cphase_data['sigmacp']
            if self.error_modeling or self.adding_syserr:
                print("Back-fetching triangle amplitudes and sigmas.")
                v1, v2, v3, v1err, v2err, v3err = get_cphase_vis_sigma(obs, cphase_data)
                cphased1, cphased2, cphased3 = cphase_uvdists(cphase_data)
                v1err = np.abs(v1err)
                v2err = np.abs(v2err)
                v3err = np.abs(v3err)
                print("Done!")
            if not(self.error_modeling):
                if self.adding_syserr:
                    _, cphase_sigma = cphase_add_syserr(v1, v2, v3, v1err, v2err, v3err, cphased1, cphased2, cphased3, fractional=self.f, additive = self.e, var_a = self.var_a, var_b=self.var_b, var_c=self.var_c, var_u0=self.var_u0)
                cphase_ln_norm = -np.sum(np.log(2.0*np.pi*ive(0, 1.0/(cphase_sigma)**2))) 
            Ncphase = len(cphase)
        def loglike(params):
            to_eval = self.build_eval(params)

            imparams = [to_eval[ipn] for ipn in self.imparam_names]
            ivecs, qvecs, uvecs, vvecs = self.compute_image(imparams)
            out = 0.
            ivec = np.sum(ivecs,axis=0)
            if self.compute_P:
                qvec = np.sum(qvecs,axis=0)
                uvec = np.sum(uvecs,axis=0)
            else:
                qvec = np.zeros_like(ivec)
                uvec = np.zeros_like(ivec)
            if self.compute_V:
                vvec = np.sum(vvecs,axis=0)
            else:
                vvec = np.zeros_like(ivec)
            self.modelim.ivec = ivec
            self.modelim.qvec = qvec
            self.modelim.uvec = uvec
            self.modelim_vvec = vvec
            self.modelim.pa = to_eval['PA']
            if 'vis' in data_types or 'qvis' in data_types or 'uvis' in data_types or 'vvis' in data_types or 'mvis' in data_types:
                model_ivis, model_qvis, model_uvis, model_vvis = self.modelim_allvis(visuv, ttype=ttype)
                if 'mvis' in data_types:
                    model_mvis = (model_qvis+1j*model_uvis)/model_ivis
                translation_phasor = np.exp(-1j*2*np.pi*(u*to_eval['xuas']+v*to_eval['yuas'])*eh.RADPERUAS)
                model_ivis = model_ivis * translation_phasor
                model_qvis = model_qvis * translation_phasor
                model_uvis = model_uvis * translation_phasor
                model_vvis = model_vvis * translation_phasor

            if 'vis' in data_types:
                if self.error_modeling:
                    _, sd = amp_add_syserr(amp, sigma, fractional=to_eval['f'], additive = to_eval['e'], var_a = to_eval['var_a'], var_b=to_eval['var_b'], var_c=to_eval['var_c'], var_u0=to_eval['var_u0'], u = uvdists)
                else:
                    sd = sigma
                # sd = sqrt(sigma**2.0 + (to_eval['f']*amp)**2.0+to_eval['e']**2.0)
                # model_vis = self.modelim_ivis(visuv, ttype=ttype)
                vislike = -0.5 * np.sum(np.abs(model_ivis-vis)**2 / sd**2)
                ln_norm = vislike-2*np.sum(np.log((2.0*np.pi)**0.5 * sd)) 
                out+=ln_norm
            if 'qvis' in data_types:
                if self.error_modeling:
                    _, sd = amp_add_syserr(qamp, qsigma, fractional=to_eval['f'], additive = to_eval['e'], var_a = to_eval['var_a'], var_b=to_eval['var_b'], var_c=to_eval['var_c'], var_u0=to_eval['var_u0'], u = uvdists)
                else:
                    sd = qsigma
                # sd = sqrt(qsigma**2.0 +(to_eval['f']*qamp)**2.0+to_eval['e']**2.0)
                qvislike = -0.5 * np.sum(np.abs(model_qvis-qvis)**2.0/sd**2)
                ln_norm = qvislike-2*np.sum(np.log((2.0*np.pi)**0.5*sd))
                out += ln_norm
            if 'uvis' in data_types:
                if self.error_modeling:
                    _, sd = amp_add_syserr(uamp, usigma, fractional=to_eval['f'], additive = to_eval['e'], var_a = to_eval['var_a'], var_b=to_eval['var_b'], var_c=to_eval['var_c'], var_u0=to_eval['var_u0'], u = uvdists)
                else:
                    sd = usigma
                # sd = sqrt(usigma**2.0 +(to_eval['f']*uamp)**2.0+to_eval['e']**2.0)
                uvislike = -0.5 * np.sum(np.abs(model_uvis-uvis)**2.0/sd**2)
                ln_norm = uvislike-2*np.sum(np.log((2.0*np.pi)**0.5*sd))
                out += ln_norm
            if 'vvis' in data_types:
                if self.error_modeling:
                    _, sd = amp_add_syserr(vamp, vsigma, fractional=to_eval['f'], additive = to_eval['e'], var_a = to_eval['var_a'], var_b=to_eval['var_b'], var_c=to_eval['var_c'], var_u0=to_eval['var_u0'], u = uvdists)
                else:
                    sd = vsigma
                # sd = sqrt(vsigma**2.0 +(to_eval['f']*vamp)**2.0+to_eval['e']**2.0)
                vvislike = -0.5 * np.sum(np.abs(model_vvis-vvis)**2.0/sd**2)
                ln_norm = vvislike-2*np.sum(np.log((2.0*np.pi)**0.5*sd))
                out += ln_norm
            if 'mvis' in data_types:
                if self.error_modeling:
                    _, sd = amp_add_syserr(amp, msigma, fractional=to_eval['f'], additive = to_eval['e'], var_a = to_eval['var_a'], var_b=to_eval['var_b'], var_c=to_eval['var_c'], var_u0=to_eval['var_u0'], u = uvdists)
                    msd = sd * np.sqrt(2/np.abs(vis)**2 + np.abs(pvis)**2 / np.abs(vis)**4)
                    mln = -2*np.sum(np.log((2.0*np.pi)**0.5*msd))
                else:
                    msd = msigma
                    mln = mvis_ln_norm
                # sd = sqrt(msigma**2.0 + (to_eval['f']*amp)**2.0+to_eval['e']**2.0)
                #sd = vsigma*sd/sigma
                mvislike = -0.5 * np.sum(np.abs(model_mvis-mvis)**2.0/msd**2)
                ln_norm = mvislike + mln
                out+=ln_norm
            if 'amp' in data_types:
                if self.error_modeling:
                    _, sd = amp_add_syserr(amp, sigma, fractional=to_eval['f'], additive = to_eval['e'], var_a = to_eval['var_a'], var_b=to_eval['var_b'], var_c=to_eval['var_c'], var_u0=to_eval['var_u0'], u = uvdists)
                else:
                    sd = sigma
                model_amp = np.abs(self.modelim_ivis(ampuv, ttype=ttype))    
                if self.rice_amps:
                    ricelike = np.sum(np.log(rice(model_amp,sd,amp)))
                    out += ricelike
                else:
                    # sd = sqrt(sigma**2.0 + (to_eval['f']*amp)**2.0+to_eval['e']**2.0)
                    # model_amp = np.abs(self.vis(ivec, rotimxvec, rotimyvec, u, v))
                    # amplike = -1/Namp * np.sum(np.abs(model_amp-amp)**2 / sd**2)
                    amplike = -0.5*np.sum((model_amp-amp)**2 / sd**2)
                    ln_norm = amplike-np.sum(np.log((2.0*np.pi)**0.5 * sd)) 
                    out+=ln_norm
            if 'logcamp' in data_types:
                if compute_minimal:
                    model_logcamp = logcamp_design_mat.dot(np.log(np.abs(self.modelim_ivis(logcamp_uvpairs,ttype=ttype))))
                else:
                    model_logcamp = self.modelim_logcamp(campuv1, campuv2, campuv3, campuv4, ttype=ttype)
                if self.error_modeling:
                    _, new_logcamp_err = logcamp_add_syserr(n1amp, n2amp, d1amp, d2amp, n1err, n2err, d1err, d2err, campd1, campd2, campd3, campd4, fractional=to_eval['f'], additive = to_eval['e'], var_a = to_eval['var_a'], var_b=to_eval['var_b'], var_c=to_eval['var_c'], var_u0=to_eval['var_u0'], debias=debias)
                    logcamplike = -0.5*np.sum((logcamp-model_logcamp)**2/new_logcamp_err**2)
                    ln_norm = logcamplike-np.sum(np.log((2.0*np.pi)**0.5 * new_logcamp_err)) 
                else:
                    logcamplike = -0.5*np.sum((logcamp-model_logcamp)**2 / logcamp_sigma**2)
                    ln_norm = logcamplike + logcamp_ln_norm
                out += ln_norm
            if 'cphase' in data_types:
                if compute_minimal:
                    model_cphase = cphase_design_mat.dot(np.angle(self.modelim_ivis(cphase_uvpairs,ttype=ttype)))
                else:
                    model_cphase = self.modelim_cphase(cphaseuv1, cphaseuv2, cphaseuv3, ttype=ttype)
                if self.error_modeling:
                    _, new_cphase_err = cphase_add_syserr(v1, v2, v3, v1err, v2err, v3err, cphased1, cphased2, cphased3, fractional=to_eval['f'], additive=to_eval['e'], var_a = to_eval['var_a'], var_b=to_eval['var_b'], var_c=to_eval['var_c'], var_u0=to_eval['var_u0'])
                    cphaselike = -np.sum((1-np.cos(cphase-model_cphase))/new_cphase_err**2)
                    ln_norm = cphaselike-np.sum(np.log(2.0*np.pi*ive(0, 1.0/(new_cphase_err)**2))) 
                else:
                    cphaselike = -np.sum((1-np.cos(cphase-model_cphase))/cphase_sigma**2)
                    ln_norm = cphaselike + cphase_ln_norm
                out += ln_norm
            return out
        print("Built combined likelihood function!")
        self.recent_loglike = loglike
        return loglike


    def KerrBam_from_eval(self, to_eval):
        new = KerrBam(self.fov, self.npix, self.jfunc, self.jarg_names, to_eval['jargs'], to_eval['MoDuas'], to_eval['a'], to_eval['inc'], to_eval['zbl'], xuas=to_eval['xuas'], yuas=to_eval['yuas'], PA=to_eval['PA'],  nmax=self.nmax, beta=to_eval['beta'], chi=to_eval['chi'], eta = to_eval['eta'], iota=to_eval['iota'], spec=to_eval['spec'], alpha_zeta=to_eval['alpha_zeta'], h = to_eval['h'], polfrac = to_eval['polfrac'], dEVPA = to_eval['dEVPA'], f=to_eval['f'], e=to_eval['e'],var_a = to_eval['var_a'], var_b = to_eval['var_b'], var_c = to_eval['var_c'], var_u0=to_eval['var_u0'],  polflux=self.polflux,source=self.source,adap_fac=self.adap_fac, interp_order=self.interp_order,axisymmetric=self.axisymmetric,stationary=self.stationary)
        return new

    def annealing_MAP(self, obs, data_types=['vis'], x0 = None, ttype='nfft', args=(), maxiter=1000,local_search_options={},initial_temp=5230.0, debias=True, seed = 4):
        """
        Given an observation and a list of data product names, 
        find the MAP using scipy's dual annealing.
        """
        self.source = obs.source
        self.modelim = eh.image.make_empty(self.npix*self.adap_fac,self.fov, ra=obs.ra, dec=obs.dec, rf= obs.rf, mjd = obs.mjd, source=obs.source)#, pulse=deltaPulse2D)
        ll = self.build_likelihood(obs, data_types=data_types,ttype=ttype, debias=debias)
        
        print("Running dual annealing...")
        res =  dual_annealing(lambda x: -ll(x), self.modeled_params, args=args, maxiter=maxiter, local_search_options=local_search_options, initial_temp=initial_temp, x0=x0,seed=seed)
        print("Done!")

        to_eval = self.build_eval(res.x)
        new = self.KerrBam_from_eval(to_eval)
        new.modelim = new.make_image(modelim=True)
        return new, res
        
    def annealing_nxcorr_MAP(self, im, args=(),  x0 = None,maxiter=1000,local_search_options={},initial_temp=5230.0, seed = 4):
        """
        Given an image, find the nxcorr MAP using scipy's dual annealing.
        """
        self.source = im.source
        self.modelim = eh.image.make_empty(self.npix*self.adap_fac,self.fov, ra=im.ra, dec=im.dec, rf= im.rf, mjd = im.mjd, source=im.source)#, pulse=deltaPulse2D)
        nn = self.build_nxcorr(im)
        print("Running dual annealing...")
        res =  dual_annealing(lambda x: -nn(x), self.modeled_params, args=args, maxiter=maxiter, local_search_options=local_search_options, initial_temp=initial_temp)
        print("Done!")

        to_eval = self.build_eval(res.x)
        new = self.KerrBam_from_eval(to_eval)
        new.modelim = new.make_image(modelim=True)
        return new, res

    def annealing_nrmse_MAP(self, im, args=(), maxiter=1000,local_search_options={},initial_temp=5230.0):
        """
        Given an image, find the nxcorr MAP using scipy's dual annealing.
        """
        self.source = im.source
        self.modelim = eh.image.make_empty(self.npix*self.adap_fac,self.fov, ra=im.ra, dec=im.dec, rf= im.rf, mjd = im.mjd, source=im.source)#, pulse=deltaPulse2D)
        nn = self.build_nrmse(im)
        print("Running dual annealing...")
        res =  dual_annealing(lambda x: nn(x), self.modeled_params, args=args, maxiter=maxiter, local_search_options=local_search_options, initial_temp=initial_temp)
        print("Done!")

        to_eval = self.build_eval(res.x)
        new = self.KerrBam_from_eval(to_eval)
        new.modelim = new.make_image(modelim=True)
        return new, res

    def build_prior_transform(self):
        functions = [get_uniform_transform(bounds[0],bounds[1]) for bounds in self.modeled_params]

        def ptform(hypercube):
            scaledcube = np.copy(hypercube)
            for i in range(len(scaledcube)):
                scaledcube[i] = functions[i](scaledcube[i])
            return scaledcube
        self.recent_ptform = ptform
        return ptform

    
    def build_sampler(self, loglike, ptform, bound='multi', sample='auto', pool=None, queue_size=None):
        sampler = dynesty.DynamicNestedSampler(loglike, ptform,self.model_dim, bound=bound, sample=sample, pool=pool, queue_size=queue_size)
        self.recent_sampler=sampler
        return sampler

    def setup(self, obs, data_types=['vis'], bound='multi', ttype='nfft', sample='auto', debias=True, pool=None, queue_size=None, compute_minimal=True, load_recent=False):
        self.source = obs.source
        self.modelim = eh.image.make_empty(self.npix*self.adap_fac,self.fov, ra=obs.ra, dec=obs.dec, rf= obs.rf, mjd = obs.mjd, source=obs.source)#, pulse=deltaPulse2D)
        ptform = self.build_prior_transform()
        loglike = self.build_likelihood(obs, data_types=data_types, ttype=ttype, debias=debias, compute_minimal=compute_minimal, load_recent=load_recent)
        sampler = self.build_sampler(loglike,ptform, bound=bound, sample=sample, pool=pool, queue_size=queue_size)
        print("Ready to model with this BAM's recent_sampler! Call run_nested!")
        return sampler

    def run_nested(self, nlive_init=500, nlive_batch =100, maxiter=None, maxcall=None, dlogz=None, logl_max=np.inf, n_effective=None, add_live=True, print_progress=True, print_func=None, save_bounds=True, maxbatch=None):
        n_effective = np.inf if n_effective is None else n_effective
        dlogz = 0.01 if dlogz is None else dlogz
        self.recent_sampler.run_nested(nlive_init=nlive_init, nlive_batch=nlive_batch,maxiter_init=maxiter,maxcall_init=maxcall,dlogz_init=dlogz,logl_max_init=logl_max, n_effective_init=n_effective, print_progress=print_progress, print_func=None, save_bounds=True, maxbatch=maxbatch)
        self.recent_results = self.recent_sampler.results
        return self.recent_results

    def run_iterated_dns(self, nlive_init=500, nlive_batch =100, maxiter=None, maxcall=None, dlogz=None, logl_max=np.inf, n_effective=None, add_live=True, print_progress=True, print_func=None, save_bounds=True, maxbatch=None, save_every_hr=np.inf, outname='./'):
        """
        Runs static nested sampling saving intermediate states. Then, runs dynamic nested sampling.
        """

        print("Running nested sampling, saving every "+str(save_every_hr)+" hour.")
        tsave = time.time()
        count = 0
        for it, res in tqdm(enumerate(self.recent_sampler.sample_initial(nlive=nlive_init,dlogz=dlogz,maxiter=maxiter,maxcall=maxcall,logl_max=logl_max,n_effective=n_effective))):
            if (time.time()-tsave)/3600 > save_every_hr:
                print('current dlogz = ',res[-1])
                # save trace plot
                tfig, taxes = dyplot.traceplot(self.recent_sampler.results,labels=self.modeled_names)
                plt.savefig(outname+'_trace_plot_'+str(count).zfill(3)+'.png',dpi=300)
                plt.close()


                # save current sampler state
                rstate = self.recent_sampler.rstate
                collect = [self.recent_sampler,rstate]
                pkl.dump(collect,open(outname+'_sampler_'+str(count).zfill(3)+'.p','wb'),protocol=pkl.HIGHEST_PROTOCOL)

               
             
          # increment count
                count += 1
                tsave = time.time()
        print("Initial static run complete. Now running dynamic nested sampling.")

        self.recent_sampler.run_nested(nlive_init=0, nlive_batch=nlive_batch,maxiter_init=maxiter,maxcall_init=maxcall,dlogz_init=dlogz,logl_max_init=logl_max, n_effective_init=n_effective, print_progress=print_progress, print_func=None, save_bounds=True, maxbatch=maxbatch)

        # print("Adding live points for dynamic nested sampling.")
        # for itf, resf in enumerate(self.recent_sampler.sample_batch(nlive_new=nlive_batch,)):
        #     # print current dlogz
        #     if (time.time()-tsave)/3600 > save_every_hr:
        #         print('current dlogz = ',resf[-1])

        #         # save trace plot
        #         tfig, taxes = dyplot.traceplot(self.recent_sampler.results)
        #         plt.savefig(outname+'_dynamic_trace_plot_'+str(count).zfill(3)+'.png',dpi=300)
        #         plt.close()

        #         # save current sampler state
        #         rstate = sampler.rstate
        #         collect = [sampler,rstate]
        #         pickle.dump(collect,open(outname+'_dynamic_sampler_'+str(count).zfill(3)+'.p','wb'),protocol=pkl.HIGHEST_PROTOCOL)

        #         # increment count
        #         count += 1
        #         tsave = time.time()
        self.recent_results = self.recent_sampler.results
        return self.recent_results

    def load_sampler(self,filename):
        sampler, rstate = pkl.load(open(filename,'rb'))
        self.recent_sampler = sampler
        self.recent_sampler.rstate = rstate

    def run_nested_default(self):
        self.recent_sampler.run_nested()
        self.recent_results = self.recent_sampler.results
        return self.recent_results
        
    def runplot(self, save='', show=True):
        fig, axes = dyplot.runplot(self.recent_results)
        if len(save)>0:
            plt.savefig(save,bbox_inches='tight')
        if show:
            plt.show()
        else:
            plt.close('all')


    def traceplot(self, save='', show=True):
        fig, axes = dyplot.traceplot(self.recent_results, labels=self.modeled_names)
        if len(save)>0:
            plt.savefig(save,bbox_inches='tight')
        if show:
            plt.show()
        else:
            plt.close('all')


    def cornerplot(self, save='',show=True, truths=None):
        fig, axes = dyplot.cornerplot(self.recent_results, labels=self.modeled_names, truths=truths)
        if len(save)>0:
            plt.savefig(save,bbox_inches='tight')
        if show:
            plt.show()
        else:
            plt.close('all')

    def mean_and_cov(self):
        samples = self.recent_results.samples
        weights = np.exp(self.recent_results.logwt - self.recent_results.logz[-1])
        return dyfunc.mean_and_cov(samples, weights)

    def save_posterior(self, outname='Bam_posterior'):
        samples = self.recent_results.samples
        weights = np.exp(self.recent_results.logwt - self.recent_results.logz[-1])
        np.savetxt(outname+'_samples.txt',samples)
        np.savetxt(outname+'_weights.txt',weights)

    def pickle_result(self, outname='results'):
        with open(outname+'.pkl','wb') as myfile:
            pkl.dump(self.recent_results, myfile)


    def MOP_Bam(self):
        mean, cov = self.mean_and_cov()
        to_eval = self.build_eval(mean)
        new = self.KerrBam_from_eval(to_eval)
        new.modelim = new.make_image(modelim=True)
        return new

    def resample_equal(self):
        samples = self.recent_results.samples
        weights = np.exp(self.recent_results.logwt - self.recent_results.logz[-1])
        resampled = dyfunc.resample_equal(samples,weights)
        return resampled

    def Bam_from_sample(self, sample):
        to_eval = self.build_eval(sample)
        new = self.KerrBam_from_eval(to_eval)
        new.modelim = new.make_image(modelim=True)
        return new

    def random_sample_Bam(self, samples=None, weights=None):
        if samples is None:
            samples = self.resample_equal()
        sample = samples[random.randint(0,len(samples)-1)]
        return self.Bam_from_sample(sample)

    def make_image(self, ra=M87_ra, dec=M87_dec, rf= 230e9, mjd = 57854, n='all', source = '', modelim=False, frame = 0):
        if source == '':
            source = self.source

        if self.mode == 'model':
            print("Cannot directly make images in model mode!")
            return
        # try:
        #     self.ivecs
        # except:
        if not self.stationary:
            print("Using frame "+str(frame))
            self.ivecs, self.qvecs, self.uvecs, self.vvecs = self.compute_image(self.imparams)[frame]
        else:
            self.ivecs, self.qvecs, self.uvecs, self.vvecs = self.compute_image(self.imparams)

        if n =='all':
            ivec = np.sum(self.ivecs,axis=0)
            if self.compute_P:
                qvec = np.sum(self.qvecs,axis=0)
                uvec = np.sum(self.uvecs,axis=0)
            else:
                qvec = np.zeros_like(ivec)
                uvec = np.zeros_like(ivec)
            if self.compute_V:
                vvec = np.sum(self.vvecs,axis=0)
            else:
                vvec = np.zeros_like(ivec)
        elif type(n) is int:
            ivec = self.ivecs[n]
            if self.compute_P:
                qvec = self.qvecs[n]
                uvec = self.uvecs[n]
            else:
                qvec = np.zeros_like(ivec)
                uvec = np.zeros_like(ivec)
            if self.compute_V:
                vvec = self.vvecs[n]
            else:
                vvec = np.zeros_like(ivec)
        
        im = eh.image.make_empty(self.npix*self.adap_fac**self.nmax,self.fov, ra=ra, dec=dec, rf= rf, mjd = mjd, source=source)#, pulse=deltaPulse2D)
        im.ivec = ivec
        im.qvec = qvec
        im.uvec = uvec
        im.vvec = vvec

        if modelim:
            im.pa = self.PA
        else:
            # im = im.rotate(self.PA)
            im.pa = self.PA
            mask = im.ivec<0
            im.ivec[mask]=0.
            im.qvec[mask]=0.
            im.uvec[mask]=0.
            im.vvec[mask]=0.

        # im.ivec *= self.tf / im.total_flux()
        return im

    def make_rotated_image(self, ra=M87_ra, dec=M87_dec, rf= 230e9, mjd = 57854, n='all', source = ''):
        out = self.make_image(ra=ra,dec=dec,rf=rf, mjd=mjd, n=n, source=source,modelim=False).rotate(self.PA)
        out.pa = 0
        return out

    def logcamp_chisq(self,obs, debias=True,compute_minimal=True,load_recent=False):
        if self.mode != 'fixed':
            print("Can only compute chisqs to fixed model!")
            return
        if self.modelim is None:
            self.modelim = self.make_image(modelim=True)
        if compute_minimal:
            if load_recent:
                logcamp_data = np.genfromtxt('logcamps.txt',dtype=None,names=['time','t1','t2','t3','t4','u1','u2','u3','u4','v1','v2','v3','v4','camp','sigmaca'])
            else:
                logcamp_data, logcamp_design_mat, logcamp_uvpairs = get_minimal_logcamps(obs)
        else:
            logcamp_data = obs.c_amplitudes(ctype='logcamp', debias=debias)
        
        # logcamp_data = obs.c_amplitudes(ctype='logcamp', debias=debias)
        sigmaca = logcamp_data['sigmaca']
        logcamp = logcamp_data['camp']
        campuv1, campuv2, campuv3, campuv4 = get_logcamp_uvpairs(logcamp_data)
        model_logcamp = self.modelim_logcamp(campuv1, campuv2, campuv3, campuv4)
        # model_logcamps = self.logcamp_fixed(logcamp_data['u1'],logcamp_data['u2'],logcamp_data['u3'],logcamp_data['u4'],logcamp_data['v1'],logcamp_data['v2'],logcamp_data['v3'],logcamp_data['v4'])
        logcamp_chisq = 1/len(sigmaca) * np.sum(np.abs((logcamp-model_logcamp)/sigmaca)**2)
        return logcamp_chisq

    def cphase_chisq(self,obs,compute_minimal=True,load_recent=False):
        if self.mode != 'fixed':
            print("Can only compute chisqs to fixed model!")
            return
        if self.modelim is None:
            self.modelim = self.make_image(modelim=True)
        if compute_minimal:
            if load_recent:
                cphase_data = np.genfromtxt('cphases.txt',dtype=None,names=['time','t1','t2','t3','u1','u2','u3','v1','v2','v3','cphase','sigmacp'])
            else:
                cphase_data, cphase_design_mat, cphase_uvpairs = get_minimal_cphases(obs)
        else:
            cphase_data = obs.c_phases(ang_unit='rad')
        # cphase_data = obs.c_phases(ang_unit='rad')
        cphase = cphase_data['cphase']
        sigmacp = cphase_data['sigmacp']
        cphaseuv1, cphaseuv2, cphaseuv3 = get_cphase_uvpairs(cphase_data)
        model_cphase = self.modelim_cphase(cphaseuv1, cphaseuv2, cphaseuv3)
        # model_cphases = self.cphase_fixed(cphase_data['u1'],cphase_data['u2'],cphase_data['u3'],cphase_data['v1'],cphase_data['v2'],cphase_data['v3'])
        cphase_chisq = (2.0/len(sigmacp)) * np.sum((1.0 - np.cos(cphase-model_cphase))/(sigmacp**2))
        return cphase_chisq

    def vis_chisq(self,obs):
        if self.mode !='fixed':
            print("Can only compute chisqs to fixed model!")
            return
        if self.modelim is None:
            self.modelim = self.make_image(modelim=True)
        u = obs.data['u']
        v = obs.data['v']
        sigma = obs.data['sigma']  
        # amp = obs.unpack('amp')['amp']
        vis = obs.data['vis']
        sd = np.sqrt(sigma**2.0)# + (self.f*amp)**2.0 + self.e**2.0)

        uv = np.vstack([u,v]).T
        model_vis = self.modelim_ivis(uv)
        # model_vis = self.vis_fixed(u,v)
        absdelta = np.abs(model_vis-vis)
        vis_chisq = np.sum((absdelta/sd)**2)/(2*len(vis))
        return vis_chisq

    def amp_chisq(self,obs,debias=True):
        if self.mode !='fixed':
            print("Can only compute chisqs to fixed model!")
            return
        if self.modelim is None:
            self.modelim = self.make_image(modelim=True)
        u = obs.data['u']
        v = obs.data['v']
        sigma = obs.data['sigma']  
        amp = obs.unpack('amp',debias=debias)['amp']
        # vis = obs.data['vis']
        sd = np.sqrt(sigma**2.0)# + (self.f*amp)**2.0 + self.e**2.0)
        uv = np.vstack([u,v]).T
        model_amp = np.abs(self.modelim_ivis(uv))
        # model_amp = np.abs(self.vis_fixed(u,v))
        absdelta = np.abs(model_amp-amp)
        amp_chisq = np.sum((absdelta/sd)**2)/(len(amp))
        return amp_chisq

    def eval_var_sys(self, u):
        return var_sys(self.var_a,self.var_b, self.var_c, self.var_u0, u)

    def all_chisqs(self, obs, debias=True):
        if self.mode !='fixed':
            print("Can only compute chisqs to fixed model!")
            return
        # chisq_obs = obs.add_fractional_noise(self.f)
        chisq_obs = obs.copy()
        obsdata = chisq_obs.unpack(['amp','uvdist'],conj=False,debias=debias)
        print("If you have specified any systematic error, it is being included in the chisq calculations.")
        chisq_obs.data['sigma'] = (chisq_obs.data['sigma']**2+(obsdata['amp']*self.f)**2+(self.e)**2 + self.eval_var_sys(obsdata['uvdist']))**0.5

        logcamp_chisq = self.logcamp_chisq(chisq_obs, debias=debias)
        cphase_chisq = self.cphase_chisq(chisq_obs)
        amp_chisq = self.amp_chisq(chisq_obs, debias=debias)
        vis_chisq = self.vis_chisq(chisq_obs)
        return {'logcamp':logcamp_chisq,'cphase':cphase_chisq,'vis':vis_chisq,'amp':amp_chisq}

    def fitsum(self, obs, outname, outdir='.', title='imgsum', commentstr="", debias=True):
        """
        In fixed mode, given the observation that was fit, produce a self-called observation
         and then pass it to the eht-imaging imgsum utility. 
        Note that sys err needs to be done manually due to allow general noise modeling.
        """
        if self.mode !='fixed':
            print("Can only produce fitsums from fixed model!")
            return
        im = self.make_rotated_image()
        new_obs = obs.copy()
        obsdata = new_obs.unpack(['amp','uvdist'], conj=False,debias=debias)
        print("If you have specified any systematic error, it is being included in the chisq calculations.")
        new_obs.data['sigma'] = (new_obs.data['sigma']**2+(obsdata['amp']*self.f)**2+(self.e)**2 + self.eval_var_sys(obsdata['uvdist']))**0.5
        im.rf = new_obs.rf
        cal_obs = self_cal(new_obs,im,ttype='nfft')
        # cal_obs = self.observe_same(new_obs,ampcal=True,phasecal=True,add_th_noise=False, seed=4)        
        imgsum(im,cal_obs,new_obs,outname,outdir=outdir,title=title,commentstr=commentstr,debias=debias)

