"""
=============================================================================
leak_detection.py
Smart Urban Water Management System — AI Leak Detection Engine

Author  : Anass Krim
Version : 1.1.0
Date    : 2025-10-01

Description:
    Implements an unsupervised anomaly detection pipeline using scikit-learn's
    IsolationForest algorithm. The detector identifies abnormal combinations of
    flow rate, pressure, turbidity, and temperature that are statistically
    inconsistent with the learned normal operational profile.

    Additionally, a pressure wave analysis module performs cross-correlation
    of simultaneous pressure readings from multiple nodes to estimate the
    distance of a suspected leak from each sensor node.

Algorithm:
    - IsolationForest: isolates anomalies by randomly partitioning data.
      Anomalies require fewer splits → shorter path length → negative score.
    - Contamination parameter: expected fraction of anomalies in training set.
    - Model is serialised with joblib for persistence across restarts.

Usage (standalone):
    python -m src.ai.leak_detection --train data/sample_data.json
    python -m src.ai.leak_detection --predict '{"flow_rate":2.1,...}'

License: MIT
=============================================================================
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================
FEATURE_NAMES = ["flow_rate", "pressure", "turbidity", "temperature"]
DEFAULT_MODEL_PATH = "models/leak_detector.joblib"
ANOMALY_LABEL = -1   # sklearn convention: -1 = anomaly, 1 = normal

# Alert severity thresholds based on anomaly score
SEVERITY_CRITICAL = -0.30   # score < -0.30 → CRITICAL
SEVERITY_WARNING  = -0.10   # score < -0.10 → WARNING
# score >= -0.10 → INFO (marginal anomaly)


# =============================================================================
# LEAK DETECTOR CLASS
# =============================================================================
class LeakDetector:
    """
    Unsupervised anomaly detector for water network sensor data.

    Wraps an IsolationForest model in a StandardScaler pipeline for
    numerical stability. Supports training, incremental-style retraining,
    single-sample prediction, and model serialisation.
    """

    def __init__(
        self,
        contamination: float = 0.05,
        n_estimators: int = 200,
        max_samples: str = "auto",
        random_state: int = 42,
        model_path: str = DEFAULT_MODEL_PATH,
    ) -> None:
        """
        Initialise the detector. Loads an existing model from disk if found.

        Args:
            contamination : Expected proportion of anomalies (0 < c < 0.5).
            n_estimators  : Number of trees in the Isolation Forest.
            max_samples   : Samples per tree ("auto" = min(256, n_samples)).
            random_state  : Random seed for reproducibility.
            model_path    : File path for model persistence.
        """
        self.contamination = contamination
        self.n_estimators  = n_estimators
        self.max_samples   = max_samples
        self.random_state  = random_state
        self.model_path    = model_path
        self._is_fitted    = False
        self._training_timestamp: Optional[str] = None
        self._training_samples: int = 0

        self._pipeline: Optional[Pipeline] = None

        # Try loading a pre-trained model
        if os.path.exists(model_path):
            try:
                self.load_model(model_path)
                logger.info(f"Model loaded from {model_path} "
                            f"(trained on {self._training_samples} samples at "
                            f"{self._training_timestamp})")
            except Exception as e:
                logger.warning(f"Could not load model from {model_path}: {e}")
                self._build_pipeline()
        else:
            self._build_pipeline()

    def _build_pipeline(self) -> None:
        """Construct a fresh StandardScaler + IsolationForest pipeline."""
        self._pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("iforest", IsolationForest(
                contamination=self.contamination,
                n_estimators=self.n_estimators,
                max_samples=self.max_samples,
                random_state=self.random_state,
                n_jobs=-1,  # Use all CPU cores
            )),
        ])
        self._is_fitted = False

    # -------------------------------------------------------------------------
    # TRAINING
    # -------------------------------------------------------------------------
    def train(self, readings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Train the IsolationForest on a list of sensor reading dicts.

        Args:
            readings: List of dicts, each containing the FEATURE_NAMES keys.

        Returns:
            Training result dict with sample count and timestamp.

        Raises:
            ValueError: If fewer than 50 samples are provided.
        """
        if len(readings) < 50:
            raise ValueError(
                f"Need at least 50 samples for training, got {len(readings)}."
            )

        X = self._extract_features(readings)
        logger.info(f"Training IsolationForest on {X.shape[0]} samples, "
                    f"{X.shape[1]} features...")

        self._pipeline.fit(X)
        self._is_fitted           = True
        self._training_timestamp  = datetime.now(timezone.utc).isoformat()
        self._training_samples    = X.shape[0]

        # Compute anomaly scores on training set for diagnostics
        scores = self._pipeline.decision_function(X)
        n_anomalies = int(np.sum(self._pipeline.predict(X) == ANOMALY_LABEL))

        result = {
            "status":          "trained",
            "samples":         X.shape[0],
            "n_anomalies_train": n_anomalies,
            "contamination":   self.contamination,
            "score_mean":      float(np.mean(scores)),
            "score_std":       float(np.std(scores)),
            "score_min":       float(np.min(scores)),
            "timestamp":       self._training_timestamp,
        }
        logger.info(f"Training complete: {result}")
        return result

    def train_from_file(self, json_path: str) -> Dict[str, Any]:
        """
        Convenience: load sample_data.json and train the model.

        Args:
            json_path: Path to a JSON file containing a list of readings.

        Returns:
            Training result dict.
        """
        with open(json_path, "r", encoding="utf-8") as f:
            readings = json.load(f)
        logger.info(f"Loaded {len(readings)} readings from {json_path}")
        return self.train(readings)

    # -------------------------------------------------------------------------
    # PREDICTION
    # -------------------------------------------------------------------------
    def predict_single(self, features: List[float]) -> Dict[str, Any]:
        """
        Run inference on a single reading.

        Args:
            features: List of 4 floats: [flow_rate, pressure, turbidity, temperature]

        Returns:
            Dict with keys: is_anomaly (bool), score (float), severity (str).

        Raises:
            RuntimeError: If the model has not been trained yet.
        """
        if not self._is_fitted:
            raise RuntimeError("Model not trained. Call train() or load_model() first.")

        X = np.array(features, dtype=float).reshape(1, -1)
        label = int(self._pipeline.predict(X)[0])
        score = float(self._pipeline.decision_function(X)[0])
        is_anomaly = label == ANOMALY_LABEL

        severity = self._score_to_severity(score) if is_anomaly else "NORMAL"

        return {
            "is_anomaly": is_anomaly,
            "label":      label,
            "score":      round(score, 6),
            "severity":   severity,
            "features":   {k: v for k, v in zip(FEATURE_NAMES, features)},
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        }

    def predict_batch(self, readings: List[Dict]) -> List[Dict[str, Any]]:
        """
        Run batch inference over a list of reading dicts.

        Args:
            readings: List of sensor reading dicts.

        Returns:
            List of result dicts, same order as input.
        """
        if not self._is_fitted:
            raise RuntimeError("Model not trained.")

        X      = self._extract_features(readings)
        labels = self._pipeline.predict(X)
        scores = self._pipeline.decision_function(X)

        results = []
        for i, reading in enumerate(readings):
            is_anom  = int(labels[i]) == ANOMALY_LABEL
            score    = float(scores[i])
            severity = self._score_to_severity(score) if is_anom else "NORMAL"
            results.append({
                "index":      i,
                "node_id":    reading.get("node_id", "unknown"),
                "timestamp":  reading.get("timestamp"),
                "is_anomaly": is_anom,
                "score":      round(score, 6),
                "severity":   severity,
            })
        return results

    # -------------------------------------------------------------------------
    # PRESSURE WAVE ANALYSIS
    # -------------------------------------------------------------------------
    def localize_leak(
        self,
        node_pressures: Dict[str, np.ndarray],
        wave_speed_ms: float = 1200.0,
        sample_rate_hz: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Estimate leak location via pressure wave cross-correlation (simplified).

        Uses the time-difference-of-arrival (TDOA) between nodes to estimate
        which pipe segment contains the leak.

        Args:
            node_pressures : Dict mapping node_id → 1D numpy array of pressure
                             time series (same length and sample rate).
            wave_speed_ms  : Speed of pressure waves in the pipe (m/s).
                             Typical range: 800–1400 m/s for steel/PVC pipes.
            sample_rate_hz : Sample rate of the pressure arrays (Hz).

        Returns:
            Dict with TDOA delays and estimated distances.
        """
        node_ids = list(node_pressures.keys())
        if len(node_ids) < 2:
            return {"error": "Need at least 2 nodes for TDOA localization."}

        results = {}
        ref_id  = node_ids[0]
        ref_sig = node_pressures[ref_id] - np.mean(node_pressures[ref_id])

        for node_id in node_ids[1:]:
            sig = node_pressures[node_id] - np.mean(node_pressures[node_id])

            # Normalised cross-correlation via FFT
            n      = len(ref_sig) + len(sig) - 1
            fft_r  = np.fft.rfft(ref_sig, n=n)
            fft_s  = np.fft.rfft(sig,     n=n)
            xcorr  = np.fft.irfft(fft_r * np.conj(fft_s), n=n)

            # Find peak lag
            lags    = np.arange(-len(ref_sig) + 1, len(sig))
            peak_idx = int(np.argmax(np.abs(xcorr)))
            delay_samples = lags[peak_idx]
            delay_seconds = delay_samples / sample_rate_hz

            # Distance estimate: d = v * Δt / 2
            distance_m = abs(wave_speed_ms * delay_seconds / 2.0)

            results[f"{ref_id}_to_{node_id}"] = {
                "delay_samples": int(delay_samples),
                "delay_seconds": round(float(delay_seconds), 4),
                "estimated_distance_m": round(distance_m, 2),
            }
            logger.info(
                f"TDOA {ref_id}→{node_id}: Δt={delay_seconds:.4f} s, "
                f"est. distance={distance_m:.1f} m"
            )

        return {
            "reference_node":  ref_id,
            "wave_speed_ms":   wave_speed_ms,
            "sample_rate_hz":  sample_rate_hz,
            "tdoa_results":    results,
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }

    # -------------------------------------------------------------------------
    # ALERT GENERATION
    # -------------------------------------------------------------------------
    def generate_alert(self, result: Dict[str, Any], node_id: str) -> Optional[Dict]:
        """
        Generate a structured alert dict from a prediction result.

        Returns None if the result is normal (not an anomaly).
        """
        if not result.get("is_anomaly"):
            return None

        return {
            "node_id":    node_id,
            "severity":   result["severity"],
            "alert_type": "AI_ANOMALY",
            "score":      result["score"],
            "features":   result.get("features", {}),
            "message": (
                f"Anomaly detected on node {node_id}: score={result['score']:.4f}, "
                f"severity={result['severity']}. "
                f"Suspected pattern — "
                f"flow={result['features'].get('flow_rate','?')} L/min, "
                f"pressure={result['features'].get('pressure','?')} kPa."
            ),
            "timestamp":  result["timestamp"],
        }

    # -------------------------------------------------------------------------
    # MODEL PERSISTENCE
    # -------------------------------------------------------------------------
    def save_model(self, path: Optional[str] = None) -> str:
        """Serialise the trained pipeline to disk using joblib."""
        if not self._is_fitted:
            raise RuntimeError("Cannot save an untrained model.")
        save_path = path or self.model_path
        os.makedirs(os.path.dirname(save_path) if os.path.dirname(save_path) else ".", exist_ok=True)
        metadata = {
            "pipeline":            self._pipeline,
            "training_timestamp":  self._training_timestamp,
            "training_samples":    self._training_samples,
            "contamination":       self.contamination,
            "feature_names":       FEATURE_NAMES,
        }
        joblib.dump(metadata, save_path, compress=3)
        logger.info(f"Model saved to {save_path}")
        return save_path

    def load_model(self, path: str) -> None:
        """Load a previously saved model from disk."""
        metadata = joblib.load(path)
        self._pipeline           = metadata["pipeline"]
        self._training_timestamp = metadata.get("training_timestamp")
        self._training_samples   = metadata.get("training_samples", 0)
        self.contamination       = metadata.get("contamination", self.contamination)
        self._is_fitted          = True

    def get_model_info(self) -> Dict[str, Any]:
        """Return metadata about the current model state."""
        return {
            "is_fitted":          self._is_fitted,
            "training_timestamp": self._training_timestamp,
            "training_samples":   self._training_samples,
            "contamination":      self.contamination,
            "n_estimators":       self.n_estimators,
            "feature_names":      FEATURE_NAMES,
            "model_path":         self.model_path,
        }

    # -------------------------------------------------------------------------
    # PRIVATE HELPERS
    # -------------------------------------------------------------------------
    def _extract_features(self, readings: List[Dict]) -> np.ndarray:
        """Extract and stack feature vectors from a list of reading dicts."""
        rows = []
        for r in readings:
            # Support both flat dicts and nested {"sensors": {...}} format
            if "sensors" in r:
                s = r["sensors"]
            else:
                s = r
            row = [float(s.get(f, 0.0)) for f in FEATURE_NAMES]
            rows.append(row)
        return np.array(rows, dtype=float)

    @staticmethod
    def _score_to_severity(score: float) -> str:
        """Map a raw anomaly score to a severity label."""
        if score < SEVERITY_CRITICAL:
            return "CRITICAL"
        elif score < SEVERITY_WARNING:
            return "WARNING"
        else:
            return "INFO"


# =============================================================================
# COMMAND-LINE INTERFACE
# =============================================================================
def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Water Leak Detection — CLI")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--train",   metavar="JSON_FILE",
                       help="Train model on readings from a JSON file")
    group.add_argument("--predict", metavar="JSON_STRING",
                       help='Predict anomaly for a single reading (JSON string)')
    group.add_argument("--info",    action="store_true",
                       help="Display model info")
    parser.add_argument("--model",  default=DEFAULT_MODEL_PATH,
                        help="Path to model file")
    parser.add_argument("--contamination", type=float, default=0.05)
    args = parser.parse_args()

    detector = LeakDetector(
        contamination=args.contamination,
        model_path=args.model,
    )

    if args.train:
        result = detector.train_from_file(args.train)
        detector.save_model()
        print(json.dumps(result, indent=2))

    elif args.predict:
        reading = json.loads(args.predict)
        features = [float(reading.get(f, 0.0)) for f in FEATURE_NAMES]
        result = detector.predict_single(features)
        print(json.dumps(result, indent=2))

    elif args.info:
        print(json.dumps(detector.get_model_info(), indent=2))


if __name__ == "__main__":
    main()
