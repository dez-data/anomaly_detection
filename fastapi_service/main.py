# main.py — Anomaly scoring microservice
#
# Run with:  uvicorn main:app --reload --port 8000
# (from inside the activated venv, in C:\anomaly-service)
#
# This replaces the n8n Python Code node entirely. n8n's job now is just to
# collect the baseline rows + the new event and POST them here as JSON;
# all the actual pandas/scikit-learn work happens in this real Python process.

from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
import pandas as pd
from sklearn.ensemble import IsolationForest

app = FastAPI()

# ── Pydantic models: these describe the SHAPE of valid input
# FastAPI checks incoming JSON against these automatically, before your
# function even runs. If a request is missing a field, or sends "abc" where
# a number is expected, the caller gets a clear 422 error back — you never
# see malformed data inside score_event() at all. This is what replaces the
# missing-field / wrong-type guards the old n8n script had to check by hand.

class DataPoint(BaseModel):
    traffic_volume: float
    response_latency: float

class ScoreRequest(BaseModel):
    baseline: List[DataPoint]   # historical "normal" rows, from Postgres
    new_event: DataPoint        # the single live event being checked

class ScoreResult(BaseModel):
    status: str                          # ANOMALY | NORMAL | INSUFFICIENT_BASELINE
    anomaly_score: Optional[float] = None
    traffic_volume: float
    response_latency: float
    reason: Optional[str] = None


MIN_BASELINE_SIZE = 30   # don't trust a model fit on fewer historical points than this
CONTAMINATION = 0.10     # assumed noise rate *within the baseline*, not a forced rate per batch


@app.get("/")
def health_check():
    # Quick sanity-check endpoint — lets you (or n8n) confirm the service is
    # up at all, separately from whether the scoring logic itself works.
    return {"status": "ok"}


@app.post("/score", response_model=ScoreResult)
def score_event(request: ScoreRequest):
    baseline_df = pd.DataFrame([point.dict() for point in request.baseline])
    features = ['traffic_volume', 'response_latency']

    # Cold-start guard: not enough history to trust a model yet.
    # Same reasoning as the n8n version — flag it plainly rather than
    # fabricate an ANOMALY/NORMAL verdict off too little data.
    if len(baseline_df) < MIN_BASELINE_SIZE:
        return ScoreResult(
            status="INSUFFICIENT_BASELINE",
            traffic_volume=request.new_event.traffic_volume,
            response_latency=request.new_event.response_latency,
            reason=f"Only {len(baseline_df)} historical points available, need {MIN_BASELINE_SIZE}+"
        )

    # Fit on HISTORICAL data only — this is the core correction from your
    # very first draft. contamination is an assumption about noise *within*
    # this baseline, not a rate forced onto every incoming batch.
    model = IsolationForest(contamination=CONTAMINATION, random_state=42)
    model.fit(baseline_df[features])

    new_point_df = pd.DataFrame([request.new_event.dict()])
    prediction = model.predict(new_point_df[features])[0]        # -1 = anomaly, 1 = normal
    score = model.decision_function(new_point_df[features])[0]   # continuous; lower = more anomalous

    return ScoreResult(
        status="ANOMALY" if prediction == -1 else "NORMAL",
        anomaly_score=float(score),
        traffic_volume=request.new_event.traffic_volume,
        response_latency=request.new_event.response_latency,
    )
