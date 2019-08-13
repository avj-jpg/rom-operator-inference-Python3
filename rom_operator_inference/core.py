# core.py
"""Class for model order reduction of ODEs via operator inference."""

import numpy as np
from scipy.integrate import solve_ivp, IntegrationWarning

from . import utils

kron2 = utils.kron_compact


class ReducedModel:
    """Reduced order model for a system of high-dimensional ODEs of the form

        dx / dt = f(t,x(t),u(t)),
           x(0) = x0.

    The model form (structure) of the desired reduced model is user specified,
    and the operators of the reduced model are inferred by solving a
    regularized ordinary least squares problem.

    Parameters
    ----------
    modelform : str
        The structure of the desired reduced-order model. Options:
        'L'    : linear model f(x) = Ax.
        'Lc'   : linear model with a constant f(x) = Ax + c.
        'Q'    : strictly quadratic model f(x) = Fx^2.
        'Qc'   : strictly quadratic model with a constant f(x) = Fx^2 + c.
        'LQ'   : linear quadratic model f(x) = Ax + Fx^2.
        'LQc'  : linear quadratic model with a constant f(x) = Ax + Fx^2 + c.

    has_inputs : bool, optional, default: False.
        If True, assume the system has an additive input term u(t).

    Attributes
    ----------
    n : int
        The dimension of the original model.

    r : int
        The dimension of the learned reduced-order model.

    m : int or None
        The dimension of the input u(t), or None if `has_inputs` is False.

    Vr : (n,r) ndarray
        The basis for the linear reduced space (e.g., POD basis matrix).

    residual_ : float
        The squared Frobenius-norm residual of the least-squares problem for
        computing the reduced-order model operators.

    A_ : (r,r) ndarray or None
        Learned ROM linear state matrix, or None if 'L' is not in `modelform`.

    F_ : (r,r(r+1)//2) ndarray or None
        Learned ROM quadratic state matrix (compact), or None if 'Q' is not
        in `modelform`. Used internally instead of the larger H_.

    H_ : (r,r**2) ndarray or None
        Learned ROM quadratic state matrix (full size), or None if 'Q' is not
        in `modelform`. Computed on they fly from F_.

    c_ : (r,) ndarray or None
        Learned ROM constant term, or None if 'c' is not in `modelform`.

    B_ : (r,m) ndarray or None
        Learned ROM input matrix, or None if `has_inputs` is False.

    f_ : func(float, (r,) ndarray) -> (r,) ndarray
        The complete ROM operator, defined by A_, F_, c_, and/or B_.
        Note the signiture is f_(t, x_); that is, f_ maps time x reduced state
        to reduced state. Calculated in fit() if `has_inputs` is False, and
        in predict() otherwise.

    sol_ : Bunch object returned by scipy.integrate.solve_ivp(), the result
        of integrating the learned ROM in predict(). For more details, see
        https://docs.scipy.org/doc/scipy/reference/integrate.html.
    """

    _VALID_MODEL_FORMS = {"L", "Lc", "Q", "Qc", "LQ", "LQc"}

    def __init__(self, modelform, has_inputs=False):
        self.modelform = modelform
        self.has_inputs = has_inputs

    def fit(self, X, Xdot, Vr, U=None, reg=0):
        """Solve for the reduced model operators via regularized least squares.

        Parameters
        ----------
        X : (n,k) ndarray
            Column-wise snapshot training data (each column is a snapshot).

        Xdot : (n,k) ndarray
            Column-wise velocity training data.

        Vr : (n,r) ndarray
            The basis for the linear reduced space (e.g., POD basis matrix).

        U : (m,k) ndarray
            Column-wise inputs corresponding to the snapshots.

        reg : float
            L2 regularization penalty. Solves min ||Do - r||_2 + reg*||Po||_2.
        TODO: P

        Returns
        -------
        self
        """
        # Verify modelform.
        if self.modelform not in self._VALID_MODEL_FORMS:
            raise ValueError(f"invalid modelform '{self.modelform}'. "
                             f"Options are {self._VALID_MODEL_FORMS}.")

        if self.has_inputs and U is None:
            raise ValueError("argument 'U' required since has_inputs=True")

        if not self.has_inputs and U is not None:
            raise ValueError("argument 'U' invalid since has_inputs=False")

        # Check and store dimensions.
        if X.shape != Xdot.shape:
            raise ValueError("X and Xdot different shapes "
                             f"({X.shape} != {Xdot.shape})")

        if X.shape[0] != Vr.shape[0]:
            raise ValueError("X and Vr not aligned, first dimension "
                             f"{X.shape[0]} != {Vr.shape[0]}")

        n,k = X.shape           # Dimension of system, number of shapshots.
        r = Vr.shape[1]         # Number of basis vectors.
        self.n, self.r, self.m = n, r, None

        # Project states and velocities to the reduced subspace.
        X_ = Vr.T @ X
        Xdot_ = Vr.T @ Xdot
        self.Vr = Vr

        # Construct the "Data matrix" D = [X^T, (X ⊗ X)^T, U^T, 1].
        D_blocks = []
        if 'L' in self.modelform:
            D_blocks.append(X_.T)

        if 'Q' in self.modelform:
            X2_ = kron2(X_)
            D_blocks.append(X2_.T)
            s = r*(r+1) // 2      # Dimension of compact Kronecker.
            if X2_.shape[0] != s:
                raise ArithmeticError("kron_compact() FAILED: incorrect size!")

        if 'c' in self.modelform:
            D_blocks.append(np.ones(k).reshape((k,1)))

        if self.has_inputs:
            if U.ndim == 1:
                U = U.reshape((-1,k))
            D_blocks.append(U.T)
            m = U.shape[0]
            self.m = m

        D = np.hstack(D_blocks)
        d = D.shape[1]

        # Solve for the reduced-order model operators.
        O = np.zeros((d, r))
        for j in range(r):
            O[:,j] = utils.normal_equations(D,
                                            Xdot_[j,:],
                                            reg,
                                            j).flatten()

        # Calculate residuals (squared Frobenius norms).
        self.residual_ = np.sum((D @ O - Xdot_.T)**2)
        # self.solution_ = np.sum(O**2)

        # Extract the reduced operators from O.
        i = 0
        if 'L' in self.modelform:
            self.A_ = O[i:i+self.r].T
            i += self.r
        else:
            self.A_ = None

        if 'Q' in self.modelform:
            self.F_ = O[i:i+s].T
            i += s
        else:
            self.F_ = None

        if 'c' in self.modelform:
            self.c_ = O[i:i+1][0]       # Note that c_ is one-dimensional.
            i += 1
        else:
            self.c_ = None

        if self.has_inputs:
            self.B_ = O[i:i+self.m].T
            i += self.m
        else:
            self.B_ = None

        if i != d:
            raise ArithmeticError("EXTRACTION FAILED: sizes don't match!")

        # Construct the complete ROM operator IF there are not control inputs.
        if not self.has_inputs:
            if self.modelform == "L":
                f_ = lambda t,x_: self.A_@x_
            elif self.modelform == "Lc":
                f_ = lambda t,x_: self.A_@x_ + self.c_
            elif self.modelform == "Q":
                f_ = lambda t,x_: self.F_@kron2(x_)
            elif self.modelform == "Qc":
                f_ = lambda t,x_: self.F_@kron2(x_) + self.c_
            elif self.modelform == "LQ":
                f_ = lambda t,x_: self.A_@x_ + self.F_@kron2(x_)
            elif self.modelform == "LQc":
                f_ = lambda t,x_: self.A_@x_ + self.F_@kron2(x_) + self.c_
            self.f_ = f_

        return self

    @property
    def H_(self):
        """Matricized quadratic tensor; operates on full Kronecker product."""
        return None if self.F_ is None else utils.F2H(self.F_)

    def predict(self, x0, t, u=None, **options):
        """Simulate the learned ROM with scipy.integrate.solve_ivp().

        Parameters
        ----------
        x0 : (n,) ndarray
            The initial (high-dimensional) state vector to begin a simulation.

        t : (T,) ndarray
            The time domain over which to integrate the reduced-order system.

        u : callable OR (m,T) ndarray
            The input as a function of time (preferred) OR the input at the
            times `t`. If given as an array, u(t) is calculated by linearly
            interpolating known data points if needed for an adaptive solver.

        options
            Arguments for solver.integrate.solve_ivp(), such as the following:
            method : str
                The solver to use to solve the reduced-order system.
                * 'RK45' (default): Explicit Runge-Kutta method of order 5(4).
                * 'RK23': Explicit Runge-Kutta method of order 3(2).
                * 'Radau': Implicit Runge-Kutta method of the Radau IIA family
                    of order 5.
                * 'BDF': Implicit multi-step variable-order (1 to 5) method
                    based on a backward differentiation formula for the
                    derivative.
                * 'LSODA': Adams/BDF method with automatic stiffness detection
                    and switching. This wraps the Fortran solver from ODEPACK.
            `max_step` : float
                The maximimum allowed integration step size.
            See https://docs.scipy.org/doc/scipy/reference/integrate.html.

        Returns
        -------
        X_ROM: (n,T) ndarray
            The reduced-order approximation to the full-order system over `t`.
        """
        # Check that the model is already trained.
        if not hasattr(self, 'B_'):
            raise AttributeError("model not trained (call fit() first)")

        # Check dimensions.
        if x0.shape[0] != self.n:
            raise ValueError("invalid initial state size "
                             f"({x0.shape[0]} != {self.n})")

        if t.ndim != 1:
            raise ValueError("time 't' must be one-dimensional")
        T = t.shape[0]

        # Verify `u` matches model specifications.
        if not self.has_inputs and u is not None:
            raise ValueError("argument 'u' invalid since has_inputs=False")

        if self.has_inputs and u is None:
            raise ValueError("argument 'u' required since has_inputs=True")

        # Project initial conditions.
        x0_ = self.Vr.T @ x0

        # Interpret control input argument `u`.
        if self.has_inputs:
            if not callable(u):         # Then u should an (m,T) array.
                U = np.atleast_2d(u.copy())
                if U.shape != (self.m,T):
                    raise ValueError("invalid input shape "
                                     f"({U.shape} != {(m,T)}")
                def u(s):               # Write an interpolator function.
                    """Interpolant for the discrete data U, aligned with t"""
                    k = np.searchsorted(t, s)
                    if np.isscalar(s):
                        if k == 0 or k == T:
                            return U[:,k]
                        elif self.m == 1:
                            return np.interp(s, t[k-1:k+1], U[0,k-1:k+1])
                        return np.row_stack([np.interp(s, t[k-1:k+1],
                                                          U[i,k-1:k+1])
                                             for i in range(self.m)])
                    else:
                        return np.array([u(ss) for ss in s])

            # Construct the ROM operator if needed (waited for control inputs).
            if self.modelform == "L":
                f_ = lambda t,x_: self.A_@x_ + self.B_@u(t)
                jac_ = lambda t,x_: self.A_
            elif self.modelform == "Lc":
                f_ = lambda t,x_: self.A_@x_ + self.c_ + self.B_@u(t)
                jac_ = lambda t,x_: self.A_
            elif self.modelform == "Q":
                f_ = lambda t,x_: self.F_@kron2(x_) + self.B_@u(t)
            elif self.modelform == "Qc":
                f_ = lambda t,x_: self.F_@kron2(x_) + self.c_ + self.B_@u(t)
            elif self.modelform == "LQ":
                f_ = lambda t,x_: self.A_@x_ + self.F_@kron2(x_) + self.B_@u(t)
            elif self.modelform == "LQc":
                f_ = lambda t,x_: self.A_@x_ + self.F_@kron2(x_) + self.c_ + self.B_@u(t)
            self.f_ = f_

        # Integrate the reduced-order model.
        self.sol_ = solve_ivp(self.f_,          # Integrate f_(t,x)
                              [t[0], t[-1]],    # over this time interval
                              x0_,              # with this initial condition
                              t_eval=t,         # evaluated at these points
                              **options)        # with these solver options.

        # Raise errors if the integration failed.
        if not self.sol_.success:
            raise IntegrationWarning(self.sol_.message)

        # Reconstruct the approximation to the full-order model.
        return self.Vr @ self.sol_.y

    def get_residual_norm(self):
        return self.residual_, self.solution_

    def __getitem__(self, key):
        """Return an operator of the learned model."""
        valid_keys = {"A_", "H_", "F_", "c_", "B_"}
        if key not in valid_keys:
            raise KeyError(f"valid keys are {', '.join(valid_keys)}")
        elif key == "A_": return self.A_
        elif key == "H_": return self.H_
        elif key == "F_": return self.F_
        elif key == "c_": return self.c_
        elif key == "B_": return self.B_

    def __str__(self):
        """String representation: the structure of the model."""

        if self.modelform not in self._VALID_MODEL_FORMS:
            raise ValueError(f"invalid modelform '{self.modelform}'. "
                             f"Options are {self._VALID_MODEL_FORMS}.")
        out = []
        if 'L' in self.modelform:
            out.append("Ax(t)")
        if 'Q' in self.modelform:
            out.append("H(x ⊗ x)(t)")
        if 'c' in self.modelform:
            out.append("c")
        if self.has_inputs:
            out.append("Bu(t)")

        return "Reduced-order model structure: dx / dt = " + " + ".join(out)



__all__ = ["ReducedModel"]
