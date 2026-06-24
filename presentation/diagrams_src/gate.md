```mermaid
flowchart LR
    ocr["OCR result (text, lines)"]
    gate["run_gate (di.gate.pipeline)"]
    result["GateResult"]
    trace["di_decision_trace audit row"]
    extract["Extraction path selector"]

    ocr --> gate
    gate --> result
    result --> trace
    result --> extract
```
