"""Doc-type classifier with a zero-dependency rules fallback.

The classifier predicts a :class:`~di.models.Classification` from raw document text. It has two
modes, chosen automatically at predict time:

1. **Trained model** — when a joblib-serialised model file is present (path passed explicitly or
   via the ``DI_CLASSIFIER_MODEL`` env var), scikit-learn + joblib are lazily imported and a
   calibrated TF-IDF (char_wb 3-5 ⊕ word 1-2) + ``CalibratedClassifierCV(LinearSVC)`` pipeline is
   used to predict with a calibrated probability.
2. **Anchor fallback** — when no model is available *or* scikit-learn is missing, the prediction
   defers to :func:`di.gate.anchors.classify_by_anchors` (imported lazily so this module imports
   without that — possibly concurrently-authored — sibling). If anchors yield nothing we return a
   neutral ``UNKNOWN`` classification with confidence ``0.0``.

The module imports cleanly with **no** optional ML dependency installed; the trained path is only
touched when a model file actually exists, and :meth:`DocTypeClassifier.train` raises a clear error
if scikit-learn is absent.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from di.models import Classification

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Env var holding a path to a joblib-serialised trained pipeline (optional).
MODEL_PATH_ENV = "DI_CLASSIFIER_MODEL"

#: Returned when neither a trained model nor any anchor matched.
UNKNOWN_DOC_TYPE = "UNKNOWN"


def _build_pipeline() -> Any:
    """Build the TF-IDF (char_wb 3-5 ⊕ word 1-2) + CalibratedClassifierCV(LinearSVC) pipeline.

    scikit-learn is imported lazily so this module never requires it at import time.
    """
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.pipeline import FeatureUnion, Pipeline
    from sklearn.svm import LinearSVC

    features = FeatureUnion(
        [
            (
                "char",
                TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    lowercase=True,
                    min_df=1,
                    sublinear_tf=True,
                ),
            ),
            (
                "word",
                TfidfVectorizer(
                    analyzer="word",
                    ngram_range=(1, 2),
                    lowercase=True,
                    min_df=1,
                    sublinear_tf=True,
                ),
            ),
        ]
    )
    clf = CalibratedClassifierCV(LinearSVC())
    return Pipeline([("features", features), ("clf", clf)])


class DocTypeClassifier:
    """Predict a document type from raw text.

    Parameters
    ----------
    model_path:
        Optional path to a joblib-serialised trained pipeline. If ``None`` the
        ``DI_CLASSIFIER_MODEL`` env var is consulted. When the resolved path does not exist (or
        scikit-learn is unavailable) the classifier transparently uses the anchor fallback.
    """

    def __init__(self, model_path: str | os.PathLike[str] | None = None) -> None:
        resolved = model_path if model_path is not None else os.environ.get(MODEL_PATH_ENV)
        self._model_path: Path | None = Path(resolved) if resolved else None
        self._model: Any | None = None
        self._model_load_failed = False

    # -- model lifecycle --------------------------------------------------
    def _load_model(self) -> Any | None:
        """Lazily load the joblib model, returning ``None`` if unavailable.

        Failures (missing deps, unreadable file) are logged once and downgrade to the fallback;
        they never raise out of :meth:`predict`.
        """
        if self._model is not None:
            return self._model
        if self._model_load_failed:
            return None
        if self._model_path is None or not self._model_path.exists():
            return None
        try:
            import joblib  # lazy: optional dependency
        except ImportError:
            logger.info("joblib unavailable; using anchor fallback classifier")
            self._model_load_failed = True
            return None
        try:
            self._model = joblib.load(self._model_path)
        except (OSError, ValueError, ImportError, ModuleNotFoundError) as e:
            logger.warning("failed to load classifier model %s: %s", self._model_path, e)
            self._model_load_failed = True
            return None
        return self._model

    # -- prediction -------------------------------------------------------
    def predict(self, text: str, lang: str = "en") -> Classification:
        """Predict the document type for ``text``.

        Uses the trained model when present; otherwise defers to the anchor classifier. Always
        returns a :class:`~di.models.Classification` (never raises for empty/unknown input).
        """
        model = self._load_model()
        if model is not None:
            try:
                return self._predict_with_model(model, text)
            except Exception as e:  # noqa: BLE001 - never let model issues break the gate
                logger.warning("model prediction failed; falling back to anchors: %s", e)
        return self._predict_with_anchors(text, lang)

    def _predict_with_model(self, model: Any, text: str) -> Classification:
        proba = None
        if hasattr(model, "predict_proba"):
            proba = model.predict_proba([text])[0]
        labels = list(getattr(model, "classes_", []))
        if proba is not None and labels:
            best_idx = max(range(len(proba)), key=lambda i: proba[i])
            doc_type = str(labels[best_idx])
            confidence = float(proba[best_idx])
        else:
            doc_type = str(model.predict([text])[0])
            confidence = 1.0
        return Classification(
            doc_type=doc_type,
            confidence=confidence,
            signals=["model:tfidf+linsvc_calibrated"],
        )

    def _predict_with_anchors(self, text: str, lang: str) -> Classification:
        """Fallback: top result from the anchor classifier (lazy sibling import)."""
        try:
            from di.gate import anchors  # lazy: sibling may be authored concurrently
        except ImportError:
            logger.info("di.gate.anchors unavailable; returning UNKNOWN classification")
            return Classification(doc_type=UNKNOWN_DOC_TYPE, confidence=0.0)

        results = anchors.classify_by_anchors(text, lang)
        top = self._top_result(results)
        if top is None:
            return Classification(doc_type=UNKNOWN_DOC_TYPE, confidence=0.0)
        if isinstance(top, Classification):
            return top
        return self._coerce_classification(top)

    @staticmethod
    def _top_result(results: Any) -> Any | None:
        """Pull the first/best entry from an anchor result (list, tuple, or single object)."""
        if results is None:
            return None
        if isinstance(results, Classification):
            return results
        if isinstance(results, (list, tuple)):
            return results[0] if results else None
        return results

    @staticmethod
    def _coerce_classification(item: Any) -> Classification:
        """Best-effort coercion of an anchor result item into a Classification.

        Accepts a ``Classification``, a mapping, or a ``(doc_type, confidence)`` style tuple so the
        fallback is robust to the concurrently-authored anchor module's exact return shape.
        """
        if isinstance(item, Classification):
            return item
        if isinstance(item, dict):
            return Classification(**{k: v for k, v in item.items() if k in Classification.model_fields})
        if isinstance(item, (list, tuple)) and item:
            doc_type = str(item[0])
            confidence = float(item[1]) if len(item) > 1 else 0.0
            return Classification(doc_type=doc_type, confidence=confidence)
        return Classification(doc_type=str(item), confidence=0.0)

    # -- training ---------------------------------------------------------
    def train(
        self,
        texts: Sequence[str],
        labels: Sequence[str],
        out_path: str | os.PathLike[str],
    ) -> Any:
        """Train the calibrated TF-IDF + LinearSVC pipeline and persist it with joblib.

        scikit-learn and joblib are imported lazily; a clear error is raised if either is missing.
        Returns the fitted pipeline and updates this instance to use it for subsequent predictions.
        """
        try:
            import joblib
        except ImportError as e:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                "training requires the optional 'ml' dependency group (scikit-learn + joblib)"
            ) from e

        if len(texts) != len(labels):
            raise ValueError("texts and labels must have the same length")

        pipeline = _build_pipeline()
        pipeline.fit(list(texts), list(labels))

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(pipeline, out)

        self._model = pipeline
        self._model_path = out
        self._model_load_failed = False
        return pipeline
