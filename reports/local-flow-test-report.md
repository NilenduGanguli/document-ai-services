# Document Intelligence — Local Flow Test Report

- **Target:** `http://localhost:8080`  ·  **Client:** `acme-bank-001`
- **Service health:** `{"status": "ok", "service": "document-intelligence"}`
- Documents are uploaded as `text/plain` and pass through the OCR text-passthrough, so the full pipeline (gate → extraction → subtree → merge → persist) runs without a live OCR engine.
- Model gateway runs in offline **stub** mode unless `RETRIEVAL_BASE_URL` is set.

## 1. Ingestion (per document)

### `passport_specimen.txt`

**Input (uploaded text):**
```
PASSPORT
REPUBLIC OF UTOPIA
Type: P   Code: UTO
Surname: ERIKSSON
Given names: ANNA MARIA
P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<
L898902C36UTO7408122F1204159ZE184226B<<<<<10
```

**SSE stage events (output):**
```json
[
  {
    "stage": "ocr",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "ocr",
    "status": "done",
    "detail": {
      "engine": "text",
      "pages": 1
    }
  },
  {
    "stage": "gate",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "gate",
    "status": "done",
    "detail": {
      "doc_type": "PASSPORT",
      "sensitivity": "LOW",
      "decision": "DETERMINISTIC_ONLY",
      "lang": "en"
    }
  },
  {
    "stage": "extract",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "extract",
    "status": "done",
    "detail": {
      "facts": 7,
      "llm": false
    }
  },
  {
    "stage": "subtree",
    "status": "done",
    "detail": {
      "nodes": 11,
      "embedded": true
    }
  },
  {
    "stage": "arep",
    "status": "done",
    "detail": {
      "reps": 0,
      "deferred": false
    }
  },
  {
    "stage": "merge",
    "status": "done",
    "detail": {
      "merged_facts": 7
    }
  },
  {
    "stage": "done",
    "status": "done",
    "detail": {
      "doc_id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
      "version_id": "89d59fc4-0d5a-4fd0-982c-01b51b467df9",
      "version_no": 1,
      "doc_type": "PASSPORT",
      "decision": "DETERMINISTIC_ONLY",
      "nodes": 11,
      "facts": 7
    }
  }
]
```

### `us_ssn_card.txt`

**Input (uploaded text):**
```
SOCIAL SECURITY ADMINISTRATION
THIS NUMBER HAS BEEN ESTABLISHED FOR
JANE A DOE
536-90-4399
Signature: Jane A Doe
```

**SSE stage events (output):**
```json
[
  {
    "stage": "ocr",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "ocr",
    "status": "done",
    "detail": {
      "engine": "text",
      "pages": 1
    }
  },
  {
    "stage": "gate",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "gate",
    "status": "done",
    "detail": {
      "doc_type": "US_SSN_CARD",
      "sensitivity": "CRITICAL",
      "decision": "DETERMINISTIC_ONLY",
      "lang": "en"
    }
  },
  {
    "stage": "extract",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "extract",
    "status": "done",
    "detail": {
      "facts": 1,
      "llm": false
    }
  },
  {
    "stage": "subtree",
    "status": "done",
    "detail": {
      "nodes": 5,
      "embedded": true
    }
  },
  {
    "stage": "arep",
    "status": "done",
    "detail": {
      "reps": 0,
      "deferred": false
    }
  },
  {
    "stage": "merge",
    "status": "done",
    "detail": {
      "merged_facts": 8
    }
  },
  {
    "stage": "done",
    "status": "done",
    "detail": {
      "doc_id": "d991b22b-b830-4c73-aa45-831728cdd7b3",
      "version_id": "d0ebfb65-c091-4abe-aa6d-ca7bde5967b9",
      "version_no": 1,
      "doc_type": "US_SSN_CARD",
      "decision": "DETERMINISTIC_ONLY",
      "nodes": 5,
      "facts": 1
    }
  }
]
```

### `mx_ine_credencial.txt`

**Input (uploaded text):**
```
INSTITUTO NACIONAL ELECTORAL
CREDENCIAL PARA VOTAR
NOMBRE GUILLERMINA HERNANDEZ GUZMAN
DOMICILIO CALLE FALSA 123 COL CENTRO
CLAVE DE ELECTOR HRGZGL56042709M400
CURP HEGG560427MVZRRL04
FECHA DE NACIMIENTO 27/04/1956
SEXO M
SECCION 1234
VIGENCIA 2030
```

**SSE stage events (output):**
```json
[
  {
    "stage": "ocr",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "ocr",
    "status": "done",
    "detail": {
      "engine": "text",
      "pages": 1
    }
  },
  {
    "stage": "gate",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "gate",
    "status": "done",
    "detail": {
      "doc_type": "MX_INE",
      "sensitivity": "CRITICAL",
      "decision": "DETERMINISTIC_ONLY",
      "lang": "es"
    }
  },
  {
    "stage": "extract",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "extract",
    "status": "done",
    "detail": {
      "facts": 4,
      "llm": false
    }
  },
  {
    "stage": "subtree",
    "status": "done",
    "detail": {
      "nodes": 8,
      "embedded": true
    }
  },
  {
    "stage": "arep",
    "status": "done",
    "detail": {
      "reps": 0,
      "deferred": false
    }
  },
  {
    "stage": "merge",
    "status": "done",
    "detail": {
      "merged_facts": 10
    }
  },
  {
    "stage": "done",
    "status": "done",
    "detail": {
      "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
      "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
      "version_no": 1,
      "doc_type": "MX_INE",
      "decision": "DETERMINISTIC_ONLY",
      "nodes": 8,
      "facts": 4
    }
  }
]
```

### `us_utility_bill.txt`

**Input (uploaded text):**
```
PACIFIC ELECTRIC UTILITY
STATEMENT OF ACCOUNT
Service Address: 742 Evergreen Terrace, Springfield, OR 97403
Account Number: 4471-2098-33
Billing Period: 2026-05-01 to 2026-05-31
Customer: Jane A Doe
Amount Due: $128.44
Due Date: 2026-06-20
Thank you for your payment.
```

**SSE stage events (output):**
```json
[
  {
    "stage": "ocr",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "ocr",
    "status": "done",
    "detail": {
      "engine": "text",
      "pages": 1
    }
  },
  {
    "stage": "gate",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "gate",
    "status": "done",
    "detail": {
      "doc_type": "UTILITY_BILL",
      "sensitivity": "LOW",
      "decision": "SEND_TO_LLM",
      "lang": "en"
    }
  },
  {
    "stage": "extract",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "extract",
    "status": "done",
    "detail": {
      "facts": 0,
      "llm": true
    }
  },
  {
    "stage": "subtree",
    "status": "done",
    "detail": {
      "nodes": 3,
      "embedded": true
    }
  },
  {
    "stage": "arep",
    "status": "done",
    "detail": {
      "reps": 5,
      "deferred": false
    }
  },
  {
    "stage": "merge",
    "status": "done",
    "detail": {
      "merged_facts": 10
    }
  },
  {
    "stage": "done",
    "status": "done",
    "detail": {
      "doc_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
      "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
      "version_no": 1,
      "doc_type": "UTILITY_BILL",
      "decision": "SEND_TO_LLM",
      "nodes": 3,
      "facts": 0
    }
  }
]
```

## 2. Documents inventory
`GET /api/v1/clients/acme-bank-001/documents` → 200
```json
{
  "client_id": "acme-bank-001",
  "count": 4,
  "documents": [
    {
      "id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
      "client_id": "acme-bank-001",
      "document_name": "us_utility_bill.txt",
      "s3_uri": null,
      "sha256": "12c358edc1f3fce256fe689efaf96bcbb3416fdd24a4161e0291ea1262a69d0f",
      "mime": "text/plain",
      "doc_type": "UTILITY_BILL",
      "doc_category": "address",
      "subject": null,
      "jurisdiction": "US",
      "lang_profile": {
        "spans": [
          {
            "end": 267,
            "lang": "en",
            "start": 0
          }
        ],
        "is_bilingual": false,
        "dominant_lang": "en"
      },
      "sensitivity_bucket": "LOW",
      "gate_decision": "SEND_TO_LLM",
      "confidence": 0.7968999743461609,
      "ocr_engine": "text",
      "page_count": 1,
      "ocr_text": "PACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nAccount Number: 4471-2098-33\nBilling Period: 2026-05-01 to 2026-05-31\nCustomer: Jane A Doe\nAmount Due: $128.44\nDue Date: 2026-06-20\nThank you for your payment.",
      "ocr_lines": [
        {
          "bbox": null,
          "page": 1,
          "text": "PACIFIC ELECTRIC UTILITY",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "STATEMENT OF ACCOUNT",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Service Address: 742 Evergreen Terrace, Springfield, OR 97403",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Account Number: 4471-2098-33",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Billing Period: 2026-05-01 to 2026-05-31",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Customer: Jane A Doe",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Amount Due: $128.44",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Due Date: 2026-06-20",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Thank you for your payment.",
          "confidence": null
        }
      ],
      "classification_signals": [],
      "created_at": "2026-06-24T11:28:00.166502Z",
      "updated_at": "2026-06-24T11:28:00.166502Z",
      "deleted_at": null
    },
    {
      "id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
      "client_id": "acme-bank-001",
      "document_name": "mx_ine_credencial.txt",
      "s3_uri": null,
      "sha256": "1799ff5992b9b08476217befda1ea22e6ae18caf33dde769aced4a1d5dd79def",
      "mime": "text/plain",
      "doc_type": "MX_INE",
      "doc_category": "identity",
      "subject": null,
      "jurisdiction": "MX",
      "lang_profile": {
        "spans": [
          {
            "end": 248,
            "lang": "es",
            "start": 0
          }
        ],
        "is_bilingual": false,
        "dominant_lang": "es"
      },
      "sensitivity_bucket": "CRITICAL",
      "gate_decision": "DETERMINISTIC_ONLY",
      "confidence": 0.875,
      "ocr_engine": "text",
      "page_count": 1,
      "ocr_text": "INSTITUTO NACIONAL ELECTORAL\nCREDENCIAL PARA VOTAR\nNOMBRE GUILLERMINA HERNANDEZ GUZMAN\nDOMICILIO CALLE FALSA 123 COL CENTRO\nCLAVE DE ELECTOR HRGZGL56042709M400\nCURP HEGG560427MVZRRL04\nFECHA DE NACIMIENTO 27/04/1956\nSEXO M\nSECCION 1234\nVIGENCIA 2030",
      "ocr_lines": [
        {
          "bbox": null,
          "page": 1,
          "text": "INSTITUTO NACIONAL ELECTORAL",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "CREDENCIAL PARA VOTAR",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "NOMBRE GUILLERMINA HERNANDEZ GUZMAN",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "DOMICILIO CALLE FALSA 123 COL CENTRO",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "CLAVE DE ELECTOR HRGZGL56042709M400",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "CURP HEGG560427MVZRRL04",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "FECHA DE NACIMIENTO 27/04/1956",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "SEXO M",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "SECCION 1234",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "VIGENCIA 2030",
          "confidence": null
        }
      ],
      "classification_signals": [],
      "created_at": "2026-06-24T11:28:00.150759Z",
      "updated_at": "2026-06-24T11:28:00.150759Z",
      "deleted_at": null
    },
    {
      "id": "d991b22b-b830-4c73-aa45-831728cdd7b3",
      "client_id": "acme-bank-001",
      "document_name": "us_ssn_card.txt",
      "s3_uri": null,
      "sha256": "9bde630ec39ea8f1538f4e9afc356ff80cc1f023c2a7472b13abfcd2ac15fff3",
      "mime": "text/plain",
      "doc_type": "US_SSN_CARD",
      "doc_category": "identity",
      "subject": null,
      "jurisdiction": "US",
      "lang_profile": {
        "spans": [
          {
            "end": 112,
            "lang": "en",
            "start": 0
          }
        ],
        "is_bilingual": false,
        "dominant_lang": "en"
      },
      "sensitivity_bucket": "CRITICAL",
      "gate_decision": "DETERMINISTIC_ONLY",
      "confidence": 0.6700999736785889,
      "ocr_engine": "text",
      "page_count": 1,
      "ocr_text": "SOCIAL SECURITY ADMINISTRATION\nTHIS NUMBER HAS BEEN ESTABLISHED FOR\nJANE A DOE\n536-90-4399\nSignature: Jane A Doe",
      "ocr_lines": [
        {
          "bbox": null,
          "page": 1,
          "text": "SOCIAL SECURITY ADMINISTRATION",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "THIS NUMBER HAS BEEN ESTABLISHED FOR",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "JANE A DOE",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "536-90-4399",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Signature: Jane A Doe",
          "confidence": null
        }
      ],
      "classification_signals": [],
      "created_at": "2026-06-24T11:28:00.122337Z",
      "updated_at": "2026-06-24T11:28:00.122337Z",
      "deleted_at": null
    },
    {
      "id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
      "client_id": "acme-bank-001",
      "document_name": "passport_specimen.txt",
      "s3_uri": null,
      "sha256": "2c6ac764e02fa0ab429a7eadb0bf5c94a12c619b7b9ca1e40a102e5ba3a08b53",
      "mime": "text/plain",
      "doc_type": "PASSPORT",
      "doc_category": "identity",
      "subject": null,
      "jurisdiction": "US",
      "lang_profile": {
        "spans": [
          {
            "end": 179,
            "lang": "en",
            "start": 0
          }
        ],
        "is_bilingual": false,
        "dominant_lang": "en"
      },
      "sensitivity_bucket": "LOW",
      "gate_decision": "DETERMINISTIC_ONLY",
      "confidence": 0.2928999960422516,
      "ocr_engine": "text",
      "page_count": 1,
      "ocr_text": "PASSPORT\nREPUBLIC OF UTOPIA\nType: P   Code: UTO\nSurname: ERIKSSON\nGiven names: ANNA MARIA\nP<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204159ZE184226B<<<<<10",
      "ocr_lines": [
        {
          "bbox": null,
          "page": 1,
          "text": "PASSPORT",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "REPUBLIC OF UTOPIA",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Type: P   Code: UTO",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Surname: ERIKSSON",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "Given names: ANNA MARIA",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<",
          "confidence": null
        },
        {
          "bbox": null,
          "page": 1,
          "text": "L898902C36UTO7408122F1204159ZE184226B<<<<<10",
          "confidence": null
        }
      ],
      "classification_signals": [],
      "created_at": "2026-06-24T11:28:00.084000Z",
      "updated_at": "2026-06-24T11:28:00.084000Z",
      "deleted_at": null
    }
  ]
}
```

## 3. Knowledge tree — unmasked vs masked (toggleable projection)
`GET /clients/acme-bank-001/tree?doc_id=6ed25c45-5164-44f5-bbef-9ecb576ca9d1&mask=false` → 200
```json
{
  "client_id": "acme-bank-001",
  "count": 8,
  "tree": [
    {
      "id": "cd141a15-8d24-4db4-8621-9dd0ec375a45",
      "parent_id": null,
      "path": "client_acme_bank_001.doctype_mx_ine.v1",
      "node_type": "document",
      "seq": 0,
      "depth": 3,
      "title": "MX_INE",
      "content": null,
      "context_prefix": null,
      "attribute_key": null,
      "value_text": null,
      "value_date": null,
      "value_num": null,
      "verification_status": "unverified",
      "confidence": 0.875,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {},
      "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
      "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
      "children": [
        {
          "id": "d2beab59-fa8e-4f29-844a-b51625081bc2",
          "parent_id": "cd141a15-8d24-4db4-8621-9dd0ec375a45",
          "path": "client_acme_bank_001.doctype_mx_ine.v1.s0",
          "node_type": "section",
          "seq": 0,
          "depth": 4,
          "title": "page 1",
          "content": "INSTITUTO NACIONAL ELECTORAL\nCREDENCIAL PARA VOTAR\nNOMBRE GUILLERMINA HERNANDEZ GUZMAN\nDOMICILIO CALLE FALSA 123 COL CENTRO\nCLAVE DE ELECTOR HRGZGL56042709M400\n\u2026 (+88 chars)",
          "context_prefix": null,
          "attribute_key": null,
          "value_text": null,
          "value_date": null,
          "value_num": null,
          "verification_status": "unverified",
          "confidence": 0.0,
          "sensitivity": "LOW",
          "valid_from": null,
          "valid_to": null,
          "provenance": {
            "bbox": null,
            "page": 1,
            "model": null,
            "char_span": null,
            "extractor": null,
            "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
            "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
            "extracted_at": null
          },
          "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
          "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
          "children": [
            {
              "id": "74ad3251-b827-47e4-8c3b-a516e22a2ed4",
              "parent_id": "d2beab59-fa8e-4f29-844a-b51625081bc2",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s0.c0",
              "node_type": "chunk",
              "seq": 0,
              "depth": 5,
              "title": null,
              "content": "INSTITUTO NACIONAL ELECTORAL\nCREDENCIAL PARA VOTAR\nNOMBRE GUILLERMINA HERNANDEZ GUZMAN\nDOMICILIO CALLE FALSA 123 COL CENTRO\nCLAVE DE ELECTOR HRGZGL56042709M400\n\u2026 (+88 chars)",
              "context_prefix": null,
              "attribute_key": null,
              "value_text": null,
              "value_date": null,
              "value_num": null,
              "verification_status": "unverified",
              "confidence": 0.0,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": 1,
                "model": null,
                "char_span": null,
                "extractor": null,
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            }
          ]
        },
        {
          "id": "181f13fd-7098-4670-aead-434c41b70c53",
          "parent_id": "cd141a15-8d24-4db4-8621-9dd0ec375a45",
          "path": "client_acme_bank_001.doctype_mx_ine.v1.s1",
          "node_type": "section",
          "seq": 1,
          "depth": 4,
          "title": "facts",
          "content": null,
          "context_prefix": null,
          "attribute_key": null,
          "value_text": null,
          "value_date": null,
          "value_num": null,
          "verification_status": "unverified",
          "confidence": 0.0,
          "sensitivity": "LOW",
          "valid_from": null,
          "valid_to": null,
          "provenance": {},
          "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
          "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
          "children": [
            {
              "id": "36a2a657-fc87-49e1-ab5a-b67226342dfe",
              "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f0",
              "node_type": "fact",
              "seq": 0,
              "depth": 5,
              "title": "id.curp",
              "content": null,
              "context_prefix": null,
              "attribute_key": "id.curp",
              "value_text": "HEGG560427MVZRRL04",
              "value_date": null,
              "value_num": null,
              "verification_status": "checksum_verified",
              "confidence": 0.9700000286102295,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": null,
                "model": null,
                "char_span": null,
                "extractor": "regex_sweep",
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            },
            {
              "id": "48dfb126-6a68-474c-9cf1-5fe3abed787e",
              "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f1",
              "node_type": "fact",
              "seq": 1,
              "depth": 5,
              "title": "identity.date_of_birth",
              "content": null,
              "context_prefix": null,
              "attribute_key": "identity.date_of_birth",
              "value_text": "1956-04-27",
              "value_date": "1956-04-27",
              "value_num": null,
              "verification_status": "checksum_verified",
              "confidence": 0.949999988079071,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": null,
                "model": null,
                "char_span": null,
                "extractor": "regex_sweep",
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            },
            {
              "id": "61fa18a1-3607-45f8-8dc6-f697fc972c50",
              "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f2",
              "node_type": "fact",
              "seq": 2,
              "depth": 5,
              "title": "identity.sex",
              "content": null,
              "context_prefix": null,
              "attribute_key": "identity.sex",
              "value_text": "F",
              "value_date": null,
              "value_num": null,
              "verification_status": "checksum_verified",
              "confidence": 0.949999988079071,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": null,
                "model": null,
                "char_span": null,
                "extractor": "regex_sweep",
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            },
            {
              "id": "72c4b6a5-3bc5-49ec-ab34-652b7a38ed68",
              "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f3",
              "node_type": "fact",
              "seq": 3,
              "depth": 5,
              "title": "id.ine_clave_elector",
              "content": null,
              "context_prefix": null,
              "attribute_key": "id.ine_clave_elector",
              "value_text": "HRGZGL56042709M400",
              "value_date": null,
              "value_num": null,
              "verification_status": "unverified",
              "confidence": 0.800000011920929,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": null,
                "model": null,
                "char_span": null,
                "extractor": "regex_sweep",
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            }
          ]
        }
      ]
    }
  ]
}
```

`GET …&mask=true` → 200  (sensitive values redacted; structure preserved)
```json
{
  "client_id": "acme-bank-001",
  "count": 8,
  "tree": [
    {
      "id": "cd141a15-8d24-4db4-8621-9dd0ec375a45",
      "parent_id": null,
      "path": "client_acme_bank_001.doctype_mx_ine.v1",
      "node_type": "document",
      "seq": 0,
      "depth": 3,
      "title": "MX_INE",
      "content": null,
      "context_prefix": null,
      "attribute_key": null,
      "value_text": null,
      "value_date": null,
      "value_num": null,
      "verification_status": "unverified",
      "confidence": 0.875,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {},
      "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
      "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
      "children": [
        {
          "id": "d2beab59-fa8e-4f29-844a-b51625081bc2",
          "parent_id": "cd141a15-8d24-4db4-8621-9dd0ec375a45",
          "path": "client_acme_bank_001.doctype_mx_ine.v1.s0",
          "node_type": "section",
          "seq": 0,
          "depth": 4,
          "title": "page 1",
          "content": "INSTITUTO NACIONAL ELECTORAL\nCREDENCIAL PARA VOTAR\nNOMBRE GUILLERMINA HERNANDEZ GUZMAN\nDOMICILIO CALLE FALSA 123 COL CENTRO\nCLAVE DE ELECTOR HRGZGL56042709M400\n\u2026 (+88 chars)",
          "context_prefix": null,
          "attribute_key": null,
          "value_text": null,
          "value_date": null,
          "value_num": null,
          "verification_status": "unverified",
          "confidence": 0.0,
          "sensitivity": "LOW",
          "valid_from": null,
          "valid_to": null,
          "provenance": {
            "bbox": null,
            "page": 1,
            "model": null,
            "char_span": null,
            "extractor": null,
            "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
            "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
            "extracted_at": null
          },
          "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
          "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
          "children": [
            {
              "id": "74ad3251-b827-47e4-8c3b-a516e22a2ed4",
              "parent_id": "d2beab59-fa8e-4f29-844a-b51625081bc2",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s0.c0",
              "node_type": "chunk",
              "seq": 0,
              "depth": 5,
              "title": null,
              "content": "INSTITUTO NACIONAL ELECTORAL\nCREDENCIAL PARA VOTAR\nNOMBRE GUILLERMINA HERNANDEZ GUZMAN\nDOMICILIO CALLE FALSA 123 COL CENTRO\nCLAVE DE ELECTOR HRGZGL56042709M400\n\u2026 (+88 chars)",
              "context_prefix": null,
              "attribute_key": null,
              "value_text": null,
              "value_date": null,
              "value_num": null,
              "verification_status": "unverified",
              "confidence": 0.0,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": 1,
                "model": null,
                "char_span": null,
                "extractor": null,
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            }
          ]
        },
        {
          "id": "181f13fd-7098-4670-aead-434c41b70c53",
          "parent_id": "cd141a15-8d24-4db4-8621-9dd0ec375a45",
          "path": "client_acme_bank_001.doctype_mx_ine.v1.s1",
          "node_type": "section",
          "seq": 1,
          "depth": 4,
          "title": "facts",
          "content": null,
          "context_prefix": null,
          "attribute_key": null,
          "value_text": null,
          "value_date": null,
          "value_num": null,
          "verification_status": "unverified",
          "confidence": 0.0,
          "sensitivity": "LOW",
          "valid_from": null,
          "valid_to": null,
          "provenance": {},
          "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
          "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
          "children": [
            {
              "id": "36a2a657-fc87-49e1-ab5a-b67226342dfe",
              "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f0",
              "node_type": "fact",
              "seq": 0,
              "depth": 5,
              "title": "id.curp",
              "content": null,
              "context_prefix": null,
              "attribute_key": "id.curp",
              "value_text": "HEGG560427MVZRRL04",
              "value_date": null,
              "value_num": null,
              "verification_status": "checksum_verified",
              "confidence": 0.9700000286102295,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": null,
                "model": null,
                "char_span": null,
                "extractor": "regex_sweep",
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            },
            {
              "id": "48dfb126-6a68-474c-9cf1-5fe3abed787e",
              "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f1",
              "node_type": "fact",
              "seq": 1,
              "depth": 5,
              "title": "identity.date_of_birth",
              "content": null,
              "context_prefix": null,
              "attribute_key": "identity.date_of_birth",
              "value_text": "1956-04-27",
              "value_date": "1956-04-27",
              "value_num": null,
              "verification_status": "checksum_verified",
              "confidence": 0.949999988079071,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": null,
                "model": null,
                "char_span": null,
                "extractor": "regex_sweep",
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            },
            {
              "id": "61fa18a1-3607-45f8-8dc6-f697fc972c50",
              "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f2",
              "node_type": "fact",
              "seq": 2,
              "depth": 5,
              "title": "identity.sex",
              "content": null,
              "context_prefix": null,
              "attribute_key": "identity.sex",
              "value_text": "F",
              "value_date": null,
              "value_num": null,
              "verification_status": "checksum_verified",
              "confidence": 0.949999988079071,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": null,
                "model": null,
                "char_span": null,
                "extractor": "regex_sweep",
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            },
            {
              "id": "72c4b6a5-3bc5-49ec-ab34-652b7a38ed68",
              "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
              "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f3",
              "node_type": "fact",
              "seq": 3,
              "depth": 5,
              "title": "id.ine_clave_elector",
              "content": null,
              "context_prefix": null,
              "attribute_key": "id.ine_clave_elector",
              "value_text": "HRGZGL56042709M400",
              "value_date": null,
              "value_num": null,
              "verification_status": "unverified",
              "confidence": 0.800000011920929,
              "sensitivity": "LOW",
              "valid_from": null,
              "valid_to": null,
              "provenance": {
                "bbox": null,
                "page": null,
                "model": null,
                "char_span": null,
                "extractor": "regex_sweep",
                "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
                "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
                "extracted_at": null
              },
              "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
              "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
              "children": []
            }
          ]
        }
      ]
    }
  ]
}
```

## 4. Merged client-level facts (cross-document)
`GET /clients/acme-bank-001/facts` → 200
```json
{
  "client_id": "acme-bank-001",
  "count": 10,
  "facts": [
    {
      "id": "5de4bc41-8805-4b96-826c-6476aa179da1",
      "client_id": "acme-bank-001",
      "attribute_key": "doc.expiry_date",
      "resolved_value": "2012-04-15",
      "value_date": "2012-04-15",
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "9ea95d80-1a3c-4f5c-8358-bbc57b5ecb1c"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "LOW"
    },
    {
      "id": "439c0897-295e-4cd5-a348-1af44a271448",
      "client_id": "acme-bank-001",
      "attribute_key": "id.curp",
      "resolved_value": "HEGG560427MVZRRL04",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9700000286102295,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "36a2a657-fc87-49e1-ab5a-b67226342dfe"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "CRITICAL"
    },
    {
      "id": "a3d35502-3f4a-425c-8a1a-1454df850d91",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.date_of_birth",
      "resolved_value": "1974-08-12",
      "value_date": "1974-08-12",
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": true,
      "needs_review": true,
      "source_fact_ids": [
        "48dfb126-6a68-474c-9cf1-5fe3abed787e",
        "ece9cf1d-759e-4eb5-ac42-5ba84c327b22"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": false,
      "sensitivity": "HIGH"
    },
    {
      "id": "92751355-6c7c-49f7-b198-31361f327feb",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.given_names",
      "resolved_value": "ANNA MARIA",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "d3dbc89b-3763-48fc-b5e7-373b35d1dad5"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "HIGH"
    },
    {
      "id": "7f0ad450-5fdf-4302-9b7d-42c9cde21742",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.nationality",
      "resolved_value": "UTO",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "6a494670-ef19-4b18-8bd3-54b11ba794e1"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "HIGH"
    },
    {
      "id": "c1823985-df34-472d-b227-c90ca35aa77e",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.sex",
      "resolved_value": "F",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "61fa18a1-3607-45f8-8dc6-f697fc972c50",
        "1582f1e6-00e4-406f-a4a4-baf7adb8b183"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "HIGH"
    },
    {
      "id": "4d3b2e49-ef7f-4fbc-9cd3-0107cbcab8ea",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.surname",
      "resolved_value": "ERIKSSON",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "8930073b-3b27-4074-a1bb-004ff1865548"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "HIGH"
    },
    {
      "id": "dbbc0d07-ce88-47b8-ac1c-2afbad625d49",
      "client_id": "acme-bank-001",
      "attribute_key": "id.ine_clave_elector",
      "resolved_value": "HRGZGL56042709M400",
      "value_date": null,
      "value_num": null,
      "confidence": 0.800000011920929,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "72c4b6a5-3bc5-49ec-ab34-652b7a38ed68"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "CRITICAL"
    },
    {
      "id": "6d03f4ec-bc4e-4bbf-9bea-43a50a733c01",
      "client_id": "acme-bank-001",
      "attribute_key": "id.passport_number",
      "resolved_value": "L898902C3",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "a40988aa-139d-4b9e-a519-2ec37b6978f4"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "CRITICAL"
    },
    {
      "id": "dfb9db4f-3391-46dc-bcc7-bb36c73f2fb1",
      "client_id": "acme-bank-001",
      "attribute_key": "id.ssn",
      "resolved_value": "536-90-4399",
      "value_date": null,
      "value_num": null,
      "confidence": 0.949999988079071,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "0d82473e-d6b7-4328-967d-0a3e9daab4f6"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "CRITICAL"
    }
  ]
}
```

`GET …?verified_only=true&mask=true` → 200
```json
{
  "client_id": "acme-bank-001",
  "count": 9,
  "facts": [
    {
      "id": "5de4bc41-8805-4b96-826c-6476aa179da1",
      "client_id": "acme-bank-001",
      "attribute_key": "doc.expiry_date",
      "resolved_value": "2012-04-15",
      "value_date": "2012-04-15",
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "9ea95d80-1a3c-4f5c-8358-bbc57b5ecb1c"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "LOW"
    },
    {
      "id": "439c0897-295e-4cd5-a348-1af44a271448",
      "client_id": "acme-bank-001",
      "attribute_key": "id.curp",
      "resolved_value": "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022RL04",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9700000286102295,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "36a2a657-fc87-49e1-ab5a-b67226342dfe"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "CRITICAL",
      "masked": true
    },
    {
      "id": "92751355-6c7c-49f7-b198-31361f327feb",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.given_names",
      "resolved_value": "\u2022\u2022\u2022\u2022\u2022\u2022ARIA",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "d3dbc89b-3763-48fc-b5e7-373b35d1dad5"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "HIGH",
      "masked": true
    },
    {
      "id": "7f0ad450-5fdf-4302-9b7d-42c9cde21742",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.nationality",
      "resolved_value": "[REDACTED]",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "6a494670-ef19-4b18-8bd3-54b11ba794e1"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "HIGH",
      "masked": true
    },
    {
      "id": "c1823985-df34-472d-b227-c90ca35aa77e",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.sex",
      "resolved_value": "[REDACTED]",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "61fa18a1-3607-45f8-8dc6-f697fc972c50",
        "1582f1e6-00e4-406f-a4a4-baf7adb8b183"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "HIGH",
      "masked": true
    },
    {
      "id": "4d3b2e49-ef7f-4fbc-9cd3-0107cbcab8ea",
      "client_id": "acme-bank-001",
      "attribute_key": "identity.surname",
      "resolved_value": "\u2022\u2022\u2022\u2022SSON",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "8930073b-3b27-4074-a1bb-004ff1865548"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "HIGH",
      "masked": true
    },
    {
      "id": "dbbc0d07-ce88-47b8-ac1c-2afbad625d49",
      "client_id": "acme-bank-001",
      "attribute_key": "id.ine_clave_elector",
      "resolved_value": "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022M400",
      "value_date": null,
      "value_num": null,
      "confidence": 0.800000011920929,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "72c4b6a5-3bc5-49ec-ab34-652b7a38ed68"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "CRITICAL",
      "masked": true
    },
    {
      "id": "6d03f4ec-bc4e-4bbf-9bea-43a50a733c01",
      "client_id": "acme-bank-001",
      "attribute_key": "id.passport_number",
      "resolved_value": "\u2022\u2022\u2022\u2022\u202202C3",
      "value_date": null,
      "value_num": null,
      "confidence": 0.9900000095367432,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "a40988aa-139d-4b9e-a519-2ec37b6978f4"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "CRITICAL",
      "masked": true
    },
    {
      "id": "dfb9db4f-3391-46dc-bcc7-bb36c73f2fb1",
      "client_id": "acme-bank-001",
      "attribute_key": "id.ssn",
      "resolved_value": "\u2022\u2022\u2022\u2022\u2022\u2022\u20224399",
      "value_date": null,
      "value_num": null,
      "confidence": 0.949999988079071,
      "conflict": false,
      "needs_review": false,
      "source_fact_ids": [
        "0d82473e-d6b7-4328-967d-0a3e9daab4f6"
      ],
      "updated_at": "2026-06-24T11:28:00.177702Z",
      "verified": true,
      "sensitivity": "CRITICAL",
      "masked": true
    }
  ]
}
```

## 5. Hybrid search (scoped to client; dense+lexical+structural)

`POST /clients/acme-bank-001/search` body=`{'query': 'passport number', 'top_k': 3}` → 200
```json
{
  "client_id": "acme-bank-001",
  "query": "passport number",
  "count": 3,
  "hits": [
    {
      "id": "9da4b1d6-58b7-416f-aeea-f5f2a6f0f883",
      "parent_id": "f99a4721-6120-4a5b-aeb3-78795bce3a89",
      "path": "client_acme_bank_001.doctype_utility_bill.v1.s0.c0",
      "node_type": "chunk",
      "seq": 0,
      "depth": 5,
      "title": null,
      "content": "PACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nAccount Number: 4471-2098-33\nBilling Period: 2026-05\u2026 (+107 chars)",
      "context_prefix": "[stub:contextual] <document>\nPACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nA",
      "attribute_key": null,
      "value_text": null,
      "value_date": null,
      "value_num": null,
      "verification_status": "unverified",
      "confidence": 0.0,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": 1,
        "model": null,
        "char_span": null,
        "extractor": null,
        "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
        "document_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
        "extracted_at": null
      },
      "doc_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
      "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
      "_rank": 1,
      "_score": 0.09433047927297494
    },
    {
      "id": "8930073b-3b27-4074-a1bb-004ff1865548",
      "parent_id": "9e546356-4b2a-463e-8d1c-3a8d976be21c",
      "path": "client_acme_bank_001.doctype_passport.v1.s1.f0",
      "node_type": "fact",
      "seq": 0,
      "depth": 5,
      "title": "identity.surname",
      "content": null,
      "context_prefix": null,
      "attribute_key": "identity.surname",
      "value_text": "ERIKSSON",
      "value_date": null,
      "value_num": null,
      "verification_status": "checksum_verified",
      "confidence": 0.9900000095367432,
      "sensitivity": "HIGH",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": null,
        "model": null,
        "char_span": null,
        "extractor": "mrz",
        "version_id": "89d59fc4-0d5a-4fd0-982c-01b51b467df9",
        "document_id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
        "extracted_at": null
      },
      "doc_id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
      "version_id": "89d59fc4-0d5a-4fd0-982c-01b51b467df9",
      "_rank": 2,
      "_score": 0.01639344262295082
    },
    {
      "id": "0a1d564e-0556-4aeb-b2f3-e89365b02826",
      "parent_id": "8aa7a5bd-4ab6-4137-a62d-717bd18ea5a1",
      "path": "client_acme_bank_001.doctype_passport.v1.s0.c0",
      "node_type": "chunk",
      "seq": 0,
      "depth": 5,
      "title": null,
      "content": "PASSPORT\nREPUBLIC OF UTOPIA\nType: P   Code: UTO\nSurname: ERIKSSON\nGiven names: ANNA MARIA\nP<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204\u2026 (+19 chars)",
      "context_prefix": null,
      "attribute_key": null,
      "value_text": null,
      "value_date": null,
      "value_num": null,
      "verification_status": "unverified",
      "confidence": 0.0,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": 1,
        "model": null,
        "char_span": null,
        "extractor": null,
        "version_id": "89d59fc4-0d5a-4fd0-982c-01b51b467df9",
        "document_id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
        "extracted_at": null
      },
      "doc_id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
      "version_id": "89d59fc4-0d5a-4fd0-982c-01b51b467df9",
      "_rank": 3,
      "_score": 0.016129032258064516
    }
  ]
}
```

`POST /clients/acme-bank-001/search` body=`{'query': 'curp date of birth', 'top_k': 3}` → 200
```json
{
  "client_id": "acme-bank-001",
  "query": "curp date of birth",
  "count": 3,
  "hits": [
    {
      "id": "9da4b1d6-58b7-416f-aeea-f5f2a6f0f883",
      "parent_id": "f99a4721-6120-4a5b-aeb3-78795bce3a89",
      "path": "client_acme_bank_001.doctype_utility_bill.v1.s0.c0",
      "node_type": "chunk",
      "seq": 0,
      "depth": 5,
      "title": null,
      "content": "PACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nAccount Number: 4471-2098-33\nBilling Period: 2026-05\u2026 (+107 chars)",
      "context_prefix": "[stub:contextual] <document>\nPACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nA",
      "attribute_key": null,
      "value_text": null,
      "value_date": null,
      "value_num": null,
      "verification_status": "unverified",
      "confidence": 0.0,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": 1,
        "model": null,
        "char_span": null,
        "extractor": null,
        "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
        "document_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
        "extracted_at": null
      },
      "doc_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
      "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
      "_rank": 1,
      "_score": 0.09478972152326198
    },
    {
      "id": "ece9cf1d-759e-4eb5-ac42-5ba84c327b22",
      "parent_id": "9e546356-4b2a-463e-8d1c-3a8d976be21c",
      "path": "client_acme_bank_001.doctype_passport.v1.s1.f4",
      "node_type": "fact",
      "seq": 4,
      "depth": 5,
      "title": "identity.date_of_birth",
      "content": null,
      "context_prefix": null,
      "attribute_key": "identity.date_of_birth",
      "value_text": "1974-08-12",
      "value_date": "1974-08-12",
      "value_num": null,
      "verification_status": "checksum_verified",
      "confidence": 0.9900000095367432,
      "sensitivity": "HIGH",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": null,
        "model": null,
        "char_span": null,
        "extractor": "mrz",
        "version_id": "89d59fc4-0d5a-4fd0-982c-01b51b467df9",
        "document_id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
        "extracted_at": null
      },
      "doc_id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
      "version_id": "89d59fc4-0d5a-4fd0-982c-01b51b467df9",
      "_rank": 2,
      "_score": 0.01639344262295082
    },
    {
      "id": "56f7c0fb-b786-4e5a-818b-d856e8be5b04",
      "parent_id": "4d6fb1a6-66d0-4666-878c-faec17355052",
      "path": "client_acme_bank_001.doctype_us_ssn_card.v1.s0.c0",
      "node_type": "chunk",
      "seq": 0,
      "depth": 5,
      "title": null,
      "content": "SOCIAL SECURITY ADMINISTRATION\nTHIS NUMBER HAS BEEN ESTABLISHED FOR\nJANE A DOE\n536-90-4399\nSignature: Jane A Doe",
      "context_prefix": null,
      "attribute_key": null,
      "value_text": null,
      "value_date": null,
      "value_num": null,
      "verification_status": "unverified",
      "confidence": 0.0,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": 1,
        "model": null,
        "char_span": null,
        "extractor": null,
        "version_id": "d0ebfb65-c091-4abe-aa6d-ca7bde5967b9",
        "document_id": "d991b22b-b830-4c73-aa45-831728cdd7b3",
        "extracted_at": null
      },
      "doc_id": "d991b22b-b830-4c73-aa45-831728cdd7b3",
      "version_id": "d0ebfb65-c091-4abe-aa6d-ca7bde5967b9",
      "_rank": 3,
      "_score": 0.016129032258064516
    }
  ]
}
```

`POST /clients/acme-bank-001/search` body=`{'query': 'electric account', 'top_k': 3}` → 200
```json
{
  "client_id": "acme-bank-001",
  "query": "electric account",
  "count": 3,
  "hits": [
    {
      "id": "9da4b1d6-58b7-416f-aeea-f5f2a6f0f883",
      "parent_id": "f99a4721-6120-4a5b-aeb3-78795bce3a89",
      "path": "client_acme_bank_001.doctype_utility_bill.v1.s0.c0",
      "node_type": "chunk",
      "seq": 0,
      "depth": 5,
      "title": null,
      "content": "PACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nAccount Number: 4471-2098-33\nBilling Period: 2026-05\u2026 (+107 chars)",
      "context_prefix": "[stub:contextual] <document>\nPACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nA",
      "attribute_key": null,
      "value_text": null,
      "value_date": null,
      "value_num": null,
      "verification_status": "unverified",
      "confidence": 0.0,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": 1,
        "model": null,
        "char_span": null,
        "extractor": null,
        "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
        "document_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
        "extracted_at": null
      },
      "doc_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
      "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
      "_rank": 1,
      "_score": 0.1264777056702625
    },
    {
      "id": "48dfb126-6a68-474c-9cf1-5fe3abed787e",
      "parent_id": "181f13fd-7098-4670-aead-434c41b70c53",
      "path": "client_acme_bank_001.doctype_mx_ine.v1.s1.f1",
      "node_type": "fact",
      "seq": 1,
      "depth": 5,
      "title": "identity.date_of_birth",
      "content": null,
      "context_prefix": null,
      "attribute_key": "identity.date_of_birth",
      "value_text": "1956-04-27",
      "value_date": "1956-04-27",
      "value_num": null,
      "verification_status": "checksum_verified",
      "confidence": 0.949999988079071,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": null,
        "model": null,
        "char_span": null,
        "extractor": "regex_sweep",
        "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
        "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
        "extracted_at": null
      },
      "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
      "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
      "_rank": 2,
      "_score": 0.01639344262295082
    },
    {
      "id": "f99a4721-6120-4a5b-aeb3-78795bce3a89",
      "parent_id": "a954b214-a6d4-49ab-90a0-7e87d477bf60",
      "path": "client_acme_bank_001.doctype_utility_bill.v1.s0",
      "node_type": "section",
      "seq": 0,
      "depth": 4,
      "title": "page 1",
      "content": "PACIFIC ELECTRIC UTILITY\nSTATEMENT OF ACCOUNT\nService Address: 742 Evergreen Terrace, Springfield, OR 97403\nAccount Number: 4471-2098-33\nBilling Period: 2026-05\u2026 (+107 chars)",
      "context_prefix": null,
      "attribute_key": null,
      "value_text": null,
      "value_date": null,
      "value_num": null,
      "verification_status": "unverified",
      "confidence": 0.0,
      "sensitivity": "LOW",
      "valid_from": null,
      "valid_to": null,
      "provenance": {
        "bbox": null,
        "page": 1,
        "model": null,
        "char_span": null,
        "extractor": null,
        "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
        "document_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
        "extracted_at": null
      },
      "doc_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
      "version_id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
      "_rank": 3,
      "_score": 0.016129032258064516
    }
  ]
}
```

## 6. Capabilities manifest + answerable-questions (self-describing)
`GET /clients/acme-bank-001/docs/e94b87ee-4578-41c0-9109-8c0bb0973bb4/manifest` → 200
```json
{
  "doc_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
  "document_name": "us_utility_bill.txt",
  "doc_type": "UTILITY_BILL",
  "jurisdiction": "US",
  "page_count": 1,
  "languages": "en",
  "sensitivity": "LOW",
  "gate_decision": "SEND_TO_LLM",
  "node_type_counts": {
    "document": 1,
    "section": 1,
    "chunk": 1
  },
  "attribute_keys": [],
  "verification_status_counts": {},
  "accessibility_rep_counts": {
    "alt_phrasing": 1,
    "hypothetical_q": 1,
    "proposition": 1,
    "summary": 1,
    "translation": 1
  },
  "answerable": true,
  "searchable": true
}
```

`GET …/answerable` → 200
```json
{
  "client_id": "acme-bank-001",
  "doc_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
  "answerable": [
    {
      "question": "[stub:fast] Write a single natural-language question that this passage would directly answer. Output only the question.\n\nPassage:\nPA",
      "knode_id": "9da4b1d6-58b7-416f-aeea-f5f2a6f0f883",
      "path": "client_acme_bank_001.doctype_utility_bill.v1.s0.c0",
      "lang": "en"
    }
  ]
}
```

## 7. Node provenance (grounding)
`GET /nodes/36a2a657-fc87-49e1-ab5a-b67226342dfe/provenance?client_id=acme-bank-001` → 200
```json
{
  "node_id": "36a2a657-fc87-49e1-ab5a-b67226342dfe",
  "client_id": "acme-bank-001",
  "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
  "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
  "node_type": "fact",
  "attribute_key": "id.curp",
  "verification_status": "checksum_verified",
  "confidence": 0.9700000286102295,
  "provenance": {
    "bbox": null,
    "page": null,
    "model": null,
    "char_span": null,
    "extractor": "regex_sweep",
    "version_id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
    "document_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
    "extracted_at": null
  }
}
```

## 8. Version delta feed
`GET /clients/acme-bank-001/changes` → 200
```json
{
  "client_id": "acme-bank-001",
  "count": 4,
  "changes": [
    {
      "id": "6a4c7e9a-c39b-48fd-ad1d-37d8cfef7ba3",
      "client_id": "acme-bank-001",
      "doc_id": "e94b87ee-4578-41c0-9109-8c0bb0973bb4",
      "version_no": 1,
      "content_hash": "12c358edc1f3fce256fe689efaf96bcbb3416fdd24a4161e0291ea1262a69d0f",
      "supersedes": null,
      "is_current": true,
      "changed_fields": [],
      "created_at": "2026-06-24T11:28:00.167384Z",
      "created_by": null,
      "document_name": "us_utility_bill.txt",
      "doc_type": "UTILITY_BILL"
    },
    {
      "id": "bc6bb761-1eee-4608-aba1-49a832a2e116",
      "client_id": "acme-bank-001",
      "doc_id": "6ed25c45-5164-44f5-bbef-9ecb576ca9d1",
      "version_no": 1,
      "content_hash": "1799ff5992b9b08476217befda1ea22e6ae18caf33dde769aced4a1d5dd79def",
      "supersedes": null,
      "is_current": true,
      "changed_fields": [],
      "created_at": "2026-06-24T11:28:00.151698Z",
      "created_by": null,
      "document_name": "mx_ine_credencial.txt",
      "doc_type": "MX_INE"
    },
    {
      "id": "d0ebfb65-c091-4abe-aa6d-ca7bde5967b9",
      "client_id": "acme-bank-001",
      "doc_id": "d991b22b-b830-4c73-aa45-831728cdd7b3",
      "version_no": 1,
      "content_hash": "9bde630ec39ea8f1538f4e9afc356ff80cc1f023c2a7472b13abfcd2ac15fff3",
      "supersedes": null,
      "is_current": true,
      "changed_fields": [],
      "created_at": "2026-06-24T11:28:00.123311Z",
      "created_by": null,
      "document_name": "us_ssn_card.txt",
      "doc_type": "US_SSN_CARD"
    },
    {
      "id": "89d59fc4-0d5a-4fd0-982c-01b51b467df9",
      "client_id": "acme-bank-001",
      "doc_id": "b8721e3c-4fe0-45c3-9fdc-fd8dda13c98d",
      "version_no": 1,
      "content_hash": "2c6ac764e02fa0ab429a7eadb0bf5c94a12c619b7b9ca1e40a102e5ba3a08b53",
      "supersedes": null,
      "is_current": true,
      "changed_fields": [],
      "created_at": "2026-06-24T11:28:00.085159Z",
      "created_by": null,
      "document_name": "passport_specimen.txt",
      "doc_type": "PASSPORT"
    }
  ]
}
```

## 9. Idempotent re-upload (versioning no-op on identical content)
Re-uploading `us_ssn_card.txt` (unchanged) — SSE:
```json
[
  {
    "stage": "ocr",
    "status": "start",
    "detail": {}
  },
  {
    "stage": "ocr",
    "status": "done",
    "detail": {
      "engine": "text",
      "pages": 1
    }
  },
  {
    "stage": "version",
    "status": "skip",
    "detail": {
      "reason": "identical content already current",
      "doc_id": "d991b22b-b830-4c73-aa45-831728cdd7b3"
    }
  },
  {
    "stage": "done",
    "status": "done",
    "detail": {
      "doc_id": "d991b22b-b830-4c73-aa45-831728cdd7b3",
      "noop": true
    }
  }
]
```
