"""HMM-based unsupervised delimiter detection.

Trains a Hidden Markov Model on raw model output sequences to discover
structural phases (thinking, delimiter, content, terminal) without
labeled data. Uses the Baum-Welch (EM) algorithm for parameter estimation
and Viterbi algorithm for decoding.

The observation vocabulary comes from the featurizer (ObsCategory enum).
The hidden states correspond to output phases (PHASE_LABELS).

After training, the model can:
  - Decode new sequences into phase labels (Viterbi)
  - Auto-label the training corpus for CRF bootstrapping
  - Be used at inference time for real-time delimiter detection

Implementation uses numpy directly for transparency and minimal
dependencies. The model is small enough (~4 states × ~30 observations)
that pure numpy is fast and dependency-free.
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from training.featurizer import (
    featurize,
    atoms_to_obs_ids,
    obs_vocabulary_size,
    obs_id_to_name,
)
from training.corpus import (
    PHASE_LABELS,
    PHASE_TO_ID,
    ID_TO_PHASE,
    TrainingExample,
    load_corpus,
    save_example,
    DEFAULT_CORPUS_PATH,
)

log = logging.getLogger("llm-mvp")


@dataclass
class HMMModel:
    """Trained HMM parameters.

    Attributes:
        n_states: Number of hidden states (= len(PHASE_LABELS))
        n_obs: Number of observation symbols (= obs_vocabulary_size())
        pi: Initial state distribution [n_states]
        A: State transition matrix [n_states × n_states]
        B: Emission probability matrix [n_states × n_obs]
        state_names: Human-readable names for each state
        obs_names: Human-readable names for each observation
        log_likelihood: Final training log-likelihood
        n_iterations: Number of EM iterations completed
    """

    n_states: int
    n_obs: int
    pi: np.ndarray      # [n_states]
    A: np.ndarray        # [n_states, n_states]
    B: np.ndarray        # [n_states, n_obs]
    state_names: list[str]
    obs_names: list[str]
    log_likelihood: float = 0.0
    n_iterations: int = 0


def _initialize_params(
    n_states: int,
    n_obs: int,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Initialize HMM parameters with informed priors.

    Rather than purely random initialization, we encode structural
    knowledge about the expected phase sequence:
      - Generation typically starts in thinking (state 0)
      - Thinking transitions to delimiter (state 1)
      - Delimiter transitions to content (state 2)
      - Content transitions to terminal (state 3) or stays
      - Terminal is absorbing

    This guides EM toward the structurally meaningful solution
    without being so rigid that it can't discover alternatives.
    """
    rng = np.random.RandomState(seed)

    if n_states == 4:
        # Informed priors for the standard 4-state model
        # [thinking, delimiter, content, terminal]
        pi = np.array([0.7, 0.05, 0.2, 0.05])
        A = np.array([
            [0.85, 0.10, 0.04, 0.01],   # thinking: mostly stays, sometimes → delimiter
            [0.02, 0.30, 0.65, 0.03],   # delimiter: short, transitions to content
            [0.01, 0.01, 0.90, 0.08],   # content: mostly stays, sometimes → terminal
            [0.01, 0.01, 0.03, 0.95],   # terminal: mostly absorbing
        ])
    else:
        # Random initialization for non-standard state counts
        pi = rng.dirichlet(np.ones(n_states))
        A = rng.dirichlet(np.ones(n_states) * 0.5, size=n_states)

    pi = pi / pi.sum()

    # Add small random perturbation to transition matrix
    noise = rng.dirichlet(np.ones(n_states) * 0.1, size=n_states)
    A = 0.9 * A + 0.1 * noise
    A = A / A.sum(axis=1, keepdims=True)

    # Emission matrix — uniform with slight bias toward expected emissions
    B = rng.dirichlet(np.ones(n_obs) * 0.5, size=n_states)

    return pi, A, B


def _forward(
    obs: np.ndarray,
    pi: np.ndarray,
    A: np.ndarray,
    B: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Forward algorithm (log-space for numerical stability).

    Returns:
        alpha: Forward probabilities [T × n_states]
        scales: Scaling factors [T] (for log-likelihood computation)
    """
    T = len(obs)
    n_states = len(pi)
    alpha = np.zeros((T, n_states))
    scales = np.zeros(T)

    # Initialize
    alpha[0] = pi * B[:, obs[0]]
    scales[0] = alpha[0].sum()
    if scales[0] > 0:
        alpha[0] /= scales[0]

    # Recurse
    for t in range(1, T):
        for j in range(n_states):
            alpha[t, j] = alpha[t - 1].dot(A[:, j]) * B[j, obs[t]]
        scales[t] = alpha[t].sum()
        if scales[t] > 0:
            alpha[t] /= scales[t]

    return alpha, scales


def _backward(
    obs: np.ndarray,
    A: np.ndarray,
    B: np.ndarray,
    scales: np.ndarray,
) -> np.ndarray:
    """Backward algorithm (scaled)."""
    T = len(obs)
    n_states = A.shape[0]
    beta = np.zeros((T, n_states))

    # Initialize
    beta[T - 1] = 1.0

    # Recurse
    for t in range(T - 2, -1, -1):
        for i in range(n_states):
            beta[t, i] = (A[i, :] * B[:, obs[t + 1]] * beta[t + 1]).sum()
        if scales[t + 1] > 0:
            beta[t] /= scales[t + 1]

    return beta


def _baum_welch_step(
    sequences: list[np.ndarray],
    pi: np.ndarray,
    A: np.ndarray,
    B: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """One iteration of Baum-Welch (EM) parameter estimation.

    Returns updated (pi, A, B) and total log-likelihood.
    """
    n_states = A.shape[0]
    n_obs = B.shape[1]

    # Accumulators
    pi_acc = np.zeros(n_states)
    A_num = np.zeros((n_states, n_states))
    A_den = np.zeros(n_states)
    B_num = np.zeros((n_states, n_obs))
    B_den = np.zeros(n_states)
    total_ll = 0.0

    for obs in sequences:
        T = len(obs)
        if T == 0:
            continue

        # Forward-backward
        alpha, scales = _forward(obs, pi, A, B)
        beta = _backward(obs, A, B, scales)

        # Log-likelihood for this sequence
        ll = np.sum(np.log(scales[scales > 0]))
        total_ll += ll

        # Gamma: P(state i at time t | observations)
        gamma = alpha * beta
        gamma_sum = gamma.sum(axis=1, keepdims=True)
        gamma_sum[gamma_sum == 0] = 1e-10
        gamma = gamma / gamma_sum

        # Xi: P(state i at t, state j at t+1 | observations)
        for t in range(T - 1):
            denom = 0.0
            xi_t = np.zeros((n_states, n_states))
            for i in range(n_states):
                for j in range(n_states):
                    xi_t[i, j] = (
                        alpha[t, i] * A[i, j] * B[j, obs[t + 1]] * beta[t + 1, j]
                    )
                    denom += xi_t[i, j]
            if denom > 0:
                xi_t /= denom
            A_num += xi_t
            A_den += gamma[t]

        # Accumulate for pi
        pi_acc += gamma[0]

        # Accumulate for B
        for t in range(T):
            B_num[:, obs[t]] += gamma[t]
            B_den += gamma[t]

    # Update parameters (with smoothing to prevent zeros)
    smoothing = 1e-6
    n_seq = len(sequences)

    new_pi = pi_acc / max(n_seq, 1) + smoothing
    new_pi /= new_pi.sum()

    new_A = (A_num + smoothing) / (A_den[:, np.newaxis] + smoothing * n_states)
    new_A /= new_A.sum(axis=1, keepdims=True)

    new_B = (B_num + smoothing) / (B_den[:, np.newaxis] + smoothing * n_obs)
    new_B /= new_B.sum(axis=1, keepdims=True)

    return new_pi, new_A, new_B, total_ll


def train_hmm(
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    n_states: int = 4,
    max_iterations: int = 100,
    convergence_threshold: float = 1e-4,
    seed: int = 42,
    model_family: str | None = None,
) -> HMMModel:
    """Train an HMM on the training corpus using Baum-Welch.

    Args:
        corpus_path: Path to training corpus JSONL.
        n_states: Number of hidden states (default 4: thinking, delimiter, content, terminal).
        max_iterations: Maximum EM iterations.
        convergence_threshold: Stop when log-likelihood improvement is below this.
        seed: Random seed for initialization.
        model_family: If set, filter corpus to this model family only.

    Returns:
        Trained HMMModel.
    """
    # Load and filter corpus
    examples = load_corpus(corpus_path)
    if model_family:
        examples = [ex for ex in examples if ex.model_family == model_family]

    if not examples:
        raise ValueError(
            f"No training examples found in {corpus_path}"
            + (f" for family '{model_family}'" if model_family else "")
        )

    log.info(
        "Training HMM: %d examples, %d states, family=%s",
        len(examples),
        n_states,
        model_family or "all",
    )

    # Convert to observation ID sequences
    n_obs = obs_vocabulary_size()
    sequences = []
    for ex in examples:
        atoms = featurize(ex.raw_text)
        obs_ids = atoms_to_obs_ids(atoms)
        sequences.append(np.array(obs_ids, dtype=np.int32))

    # Initialize parameters
    pi, A, B = _initialize_params(n_states, n_obs, seed)

    # EM iterations
    prev_ll = float("-inf")
    for iteration in range(max_iterations):
        pi, A, B, ll = _baum_welch_step(sequences, pi, A, B)

        improvement = ll - prev_ll
        log.info(
            "  EM iteration %d: log-likelihood = %.4f (Δ = %.6f)",
            iteration + 1,
            ll,
            improvement,
        )

        if iteration > 0 and abs(improvement) < convergence_threshold:
            log.info("  Converged after %d iterations.", iteration + 1)
            break

        prev_ll = ll

    # Build observation names
    obs_names = [obs_id_to_name(i) for i in range(n_obs)]

    model = HMMModel(
        n_states=n_states,
        n_obs=n_obs,
        pi=pi,
        A=A,
        B=B,
        state_names=(
            PHASE_LABELS[:n_states] if n_states <= len(PHASE_LABELS)
            else PHASE_LABELS + [f"state_{i}" for i in range(len(PHASE_LABELS), n_states)]
        ),
        obs_names=obs_names,
        log_likelihood=ll,
        n_iterations=iteration + 1,
    )

    log.info(
        "HMM training complete: %d states, %d obs symbols, "
        "final log-likelihood = %.4f",
        n_states,
        n_obs,
        ll,
    )

    return model


def viterbi_decode(
    model: HMMModel,
    obs_ids: np.ndarray | list[int],
) -> list[str]:
    """Decode an observation sequence into phase labels using Viterbi.

    Args:
        model: Trained HMMModel.
        obs_ids: Integer observation IDs from atoms_to_obs_ids().

    Returns:
        List of phase label strings, one per observation.
    """
    obs = np.array(obs_ids, dtype=np.int32)
    T = len(obs)
    n_states = model.n_states

    if T == 0:
        return []

    # Log probabilities for numerical stability
    log_pi = np.log(model.pi + 1e-10)
    log_A = np.log(model.A + 1e-10)
    log_B = np.log(model.B + 1e-10)

    # Viterbi tables
    V = np.zeros((T, n_states))
    backpointers = np.zeros((T, n_states), dtype=np.int32)

    # Initialize
    V[0] = log_pi + log_B[:, obs[0]]

    # Recurse
    for t in range(1, T):
        for j in range(n_states):
            scores = V[t - 1] + log_A[:, j]
            best_i = np.argmax(scores)
            V[t, j] = scores[best_i] + log_B[j, obs[t]]
            backpointers[t, j] = best_i

    # Backtrace
    path = np.zeros(T, dtype=np.int32)
    path[T - 1] = np.argmax(V[T - 1])
    for t in range(T - 2, -1, -1):
        path[t] = backpointers[t + 1, path[t + 1]]

    return [model.state_names[s] for s in path]


def decode_text(model: HMMModel, raw_text: str) -> list[tuple[str, str]]:
    """Decode raw model output into (text, phase) pairs.

    Convenience wrapper that featurizes, decodes, and zips results.

    Returns:
        List of (atom_text, phase_label) tuples.
    """
    atoms = featurize(raw_text)
    obs_ids = atoms_to_obs_ids(atoms)
    labels = viterbi_decode(model, obs_ids)
    return [(atom.text, label) for atom, label in zip(atoms, labels)]


def extract_content(model: HMMModel, raw_text: str) -> str:
    """Extract the 'content' phase from raw model output.

    This is the inference-time function that replaces _strip_delimiter.
    Returns only the text labeled as 'content' phase.
    """
    decoded = decode_text(model, raw_text)
    content_parts = [text for text, label in decoded if label == "content"]
    return "".join(content_parts).strip()


def extract_phases(model: HMMModel, raw_text: str) -> dict[str, str]:
    """Extract all phases from raw model output.

    Returns a dict with keys from PHASE_LABELS mapping to
    the concatenated text for each phase.
    """
    decoded = decode_text(model, raw_text)
    phases: dict[str, list[str]] = {label: [] for label in PHASE_LABELS}
    for text, label in decoded:
        if label in phases:
            phases[label].append(text)
    return {k: "".join(v).strip() for k, v in phases.items()}


# ── Persistence ───────────────────────────────────────────────────────

DEFAULT_MODEL_PATH = Path("data/delim_model.pkl")


def save_model(model: HMMModel, path: Path = DEFAULT_MODEL_PATH) -> None:
    """Save a trained HMM model to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(model, f)
    log.info("HMM model saved to %s", path)


def load_model(path: Path = DEFAULT_MODEL_PATH) -> HMMModel | None:
    """Load a trained HMM model from disk. Returns None if not found."""
    if not path.exists():
        return None
    with open(path, "rb") as f:
        model = pickle.load(f)
    if isinstance(model, HMMModel):
        return model
    log.warning("Loaded object from %s is not an HMMModel", path)
    return None


# ── Auto-labeling ─────────────────────────────────────────────────────

def auto_label_corpus(
    model: HMMModel,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    output_path: Path | None = None,
    model_family: str | None = None,
) -> int:
    """Auto-label a corpus using Viterbi decoding.

    Reads examples, decodes each with the trained HMM, writes
    labeled examples to the output path. Preserves existing labels
    from non-HMM sources (human corrections).

    Returns the number of examples labeled.
    """
    output_path = output_path or corpus_path.with_suffix(".labeled.jsonl")
    examples = load_corpus(corpus_path)
    if model_family:
        examples = [ex for ex in examples if ex.model_family == model_family]

    labeled_count = 0
    for ex in examples:
        # Skip if already human-labeled
        if ex.label_source == "human":
            save_example(ex, output_path)
            continue

        atoms = featurize(ex.raw_text)
        obs_ids = atoms_to_obs_ids(atoms)
        labels = viterbi_decode(model, obs_ids)

        ex.labels = labels
        ex.label_source = "hmm_viterbi"
        save_example(ex, output_path)
        labeled_count += 1

    log.info("Auto-labeled %d examples → %s", labeled_count, output_path)
    return labeled_count


# ── Diagnostics ───────────────────────────────────────────────────────

def print_model_summary(model: HMMModel) -> None:
    """Print a human-readable summary of the trained HMM."""
    print(f"\n{'='*60}")
    print(f"HMM Model Summary")
    print(f"{'='*60}")
    print(f"States:      {model.n_states} ({', '.join(model.state_names)})")
    print(f"Observations: {model.n_obs}")
    print(f"Log-likelihood: {model.log_likelihood:.4f}")
    print(f"EM iterations:  {model.n_iterations}")

    print(f"\nInitial state distribution (π):")
    for i, name in enumerate(model.state_names):
        print(f"  {name:12s}: {model.pi[i]:.4f}")

    print(f"\nTransition matrix (A):")
    header = "  " + " ".join(f"{n:>10s}" for n in model.state_names)
    print(header)
    for i, name in enumerate(model.state_names):
        row = " ".join(f"{model.A[i, j]:10.4f}" for j in range(model.n_states))
        print(f"  {name:10s} {row}")

    print(f"\nTop emissions per state:")
    for i, name in enumerate(model.state_names):
        top_indices = np.argsort(model.B[i])[::-1][:8]
        emissions = [
            f"{model.obs_names[j]}={model.B[i, j]:.3f}"
            for j in top_indices
            if model.B[i, j] > 0.01
        ]
        print(f"  {name:12s}: {', '.join(emissions)}")
    print()
