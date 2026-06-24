"""Document taxonomy, classifier anchors, and the canonical attribute-key catalog.

Scope: US, Canada, Mexico (English + Spanish). This is *data*, not logic — the gate's Stage-0
anchor classifier, the doc-type classifier label set, and the deterministic extractor registry
all read from here. Required-document lists are intentionally config/data-driven (regulatory
drift). Extend by adding ``DocTypeSpec`` entries; nothing else needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Canonical attribute keys (dotted namespaces) — the merge view groups facts by these.
# ---------------------------------------------------------------------------
ATTRIBUTE_KEYS: dict[str, str] = {
    # identity
    "identity.full_name": "Full legal name",
    "identity.given_names": "Given name(s)",
    "identity.surname": "Surname / family name",
    "identity.date_of_birth": "Date of birth",
    "identity.sex": "Sex / gender marker",
    "identity.nationality": "Nationality",
    "identity.place_of_birth": "Place of birth",
    # government identifiers
    "id.passport_number": "Passport number",
    "id.ssn": "US Social Security Number",
    "id.itin": "US ITIN",
    "id.sin": "Canadian Social Insurance Number",
    "id.curp": "Mexican CURP",
    "id.rfc": "Mexican RFC",
    "id.ine_clave_elector": "Mexican INE Clave de Elector",
    "id.driver_license": "Driver licence number",
    "id.ein": "US Employer Identification Number",
    "id.business_number": "Canadian Business Number",
    # address
    "address.residential": "Residential address",
    "address.mailing": "Mailing address",
    "address.registered": "Registered/fiscal address",
    "address.postal_code": "Postal/ZIP code",
    # financial / income
    "income.employer": "Employer name",
    "income.amount": "Declared/observed income",
    "account.number": "Account number",
    "account.balance": "Reported balance",
    # corporate / ownership
    "entity.legal_name": "Company legal name / razón social",
    "entity.incorporation_date": "Incorporation/constitution date",
    "ownership.beneficial_owner": "Beneficial owner (>=25% / control)",
    "ownership.director": "Director / officer",
    "ownership.authorized_signer": "Authorized signatory",
    # document meta
    "doc.issue_date": "Document issue date",
    "doc.expiry_date": "Document expiry/validity date",
}


@dataclass(frozen=True)
class DocTypeSpec:
    code: str                              # stable doc_type code, e.g. "US_PASSPORT"
    label: str
    category: str                          # identity | address | income | tax | corporate | bank_form
    jurisdictions: tuple[str, ...]         # subset of {US, CA, MX}
    applies_to: str = "individual"         # individual | corporate | both
    aliases: tuple[str, ...] = ()
    anchors_en: tuple[str, ...] = ()        # high-specificity English header/keyword strings
    anchors_es: tuple[str, ...] = ()        # Spanish anchors (MX / bilingual US)
    id_patterns: tuple[str, ...] = ()       # regex hints (informational; real regexes live in extractors)
    fixed_format: bool = False              # rigid machine-readable structure (MRZ / checksummed ID)
    deterministic: bool = False             # core fields extractable without an LLM
    attribute_keys: tuple[str, ...] = ()    # canonical keys this doc-type typically yields


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------
DOC_TYPES: tuple[DocTypeSpec, ...] = (
    # ---- Universal identity ----
    DocTypeSpec(
        "PASSPORT", "Passport", "identity", ("US", "CA", "MX"),
        anchors_en=("PASSPORT", "TYPE P", "P<"),
        anchors_es=("PASAPORTE", "ESTADOS UNIDOS MEXICANOS"),
        id_patterns=(r"P<[A-Z]{3}",), fixed_format=True, deterministic=True,
        attribute_keys=(
            "identity.surname", "identity.given_names", "identity.date_of_birth",
            "identity.sex", "identity.nationality", "id.passport_number", "doc.expiry_date",
        ),
    ),
    # ---- United States ----
    DocTypeSpec(
        "US_SSN_CARD", "US Social Security Card", "identity", ("US",),
        anchors_en=("SOCIAL SECURITY", "SOCIAL SECURITY ADMINISTRATION"),
        id_patterns=(r"\d{3}-\d{2}-\d{4}",), fixed_format=True, deterministic=True,
        attribute_keys=("id.ssn", "identity.full_name"),
    ),
    DocTypeSpec(
        "US_DRIVER_LICENSE", "US Driver License", "identity", ("US",),
        aliases=("DL", "driver's license"),
        anchors_en=("DRIVER LICENSE", "DRIVER'S LICENSE", "DL", "USA"),
        deterministic=True,
        attribute_keys=(
            "id.driver_license", "identity.full_name", "identity.date_of_birth",
            "address.residential", "doc.expiry_date",
        ),
    ),
    DocTypeSpec(
        "US_EIN_LETTER", "US EIN Letter (CP-575)", "tax", ("US",), applies_to="corporate",
        anchors_en=("EMPLOYER IDENTIFICATION NUMBER", "EIN", "CP 575", "INTERNAL REVENUE SERVICE"),
        id_patterns=(r"\d{2}-\d{7}",), deterministic=True,
        attribute_keys=("id.ein", "entity.legal_name"),
    ),
    DocTypeSpec(
        "US_W2", "US Form W-2", "income", ("US",),
        anchors_en=("W-2", "WAGE AND TAX STATEMENT", "OMB No. 1545-0008"),
        deterministic=True,
        attribute_keys=("income.employer", "income.amount", "id.ssn", "id.ein"),
    ),
    DocTypeSpec(
        "US_1099", "US Form 1099", "income", ("US",),
        anchors_en=("1099", "NONEMPLOYEE COMPENSATION", "MISCELLANEOUS INCOME"),
        deterministic=True, attribute_keys=("income.amount", "id.ein", "id.ssn"),
    ),
    # ---- Canada ----
    DocTypeSpec(
        "CA_DRIVER_LICENSE", "Canadian Driver's Licence", "identity", ("CA",),
        anchors_en=("DRIVER'S LICENCE", "PERMIS DE CONDUIRE"),
        deterministic=True,
        attribute_keys=(
            "id.driver_license", "identity.full_name", "identity.date_of_birth",
            "address.residential", "doc.expiry_date",
        ),
    ),
    DocTypeSpec(
        "CA_SIN", "Canadian SIN", "identity", ("CA",),
        anchors_en=("SOCIAL INSURANCE NUMBER", "NUMÉRO D'ASSURANCE SOCIALE", "SIN"),
        id_patterns=(r"\d{3}-?\d{3}-?\d{3}",), fixed_format=True, deterministic=True,
        attribute_keys=("id.sin", "identity.full_name"),
    ),
    DocTypeSpec(
        "CA_T4", "Canadian T4", "income", ("CA",),
        anchors_en=("T4", "STATEMENT OF REMUNERATION", "ÉTAT DE LA RÉMUNÉRATION"),
        deterministic=True, attribute_keys=("income.employer", "income.amount", "id.sin"),
    ),
    DocTypeSpec(
        "CA_NOA", "Canadian Notice of Assessment", "tax", ("CA",),
        anchors_en=("NOTICE OF ASSESSMENT", "AVIS DE COTISATION", "CRA"),
        deterministic=True, attribute_keys=("income.amount", "address.mailing"),
    ),
    DocTypeSpec(
        "CA_BUSINESS_NUMBER", "Canadian Business Number", "tax", ("CA",), applies_to="corporate",
        anchors_en=("BUSINESS NUMBER", "NUMÉRO D'ENTREPRISE"),
        id_patterns=(r"\d{9}(RT|RP|RC)?\d{0,4}",), deterministic=True,
        attribute_keys=("id.business_number", "entity.legal_name"),
    ),
    # ---- Mexico ----
    DocTypeSpec(
        "MX_INE", "Mexican INE/IFE Credential", "identity", ("MX",),
        aliases=("IFE", "credencial para votar"),
        anchors_es=("INSTITUTO NACIONAL ELECTORAL", "CREDENCIAL PARA VOTAR", "CLAVE DE ELECTOR"),
        id_patterns=(r"IDMEX", r"[A-Z]{6}\d{6}\d{2}[HM]\d{3}"),
        fixed_format=True, deterministic=True,
        attribute_keys=(
            "id.ine_clave_elector", "id.curp", "identity.full_name",
            "identity.date_of_birth", "identity.sex", "address.residential",
        ),
    ),
    DocTypeSpec(
        "MX_CURP", "Mexican CURP", "identity", ("MX",),
        anchors_es=("CLAVE ÚNICA DE REGISTRO DE POBLACIÓN", "CURP", "RENAPO"),
        id_patterns=(r"[A-Z]{4}\d{6}[HM][A-Z]{5}[0-9A-Z]\d",),
        fixed_format=True, deterministic=True,
        attribute_keys=("id.curp", "identity.date_of_birth", "identity.sex"),
    ),
    DocTypeSpec(
        "MX_RFC_CSF", "Mexican RFC / Constancia de Situación Fiscal", "tax", ("MX",),
        applies_to="both",
        anchors_es=(
            "CONSTANCIA DE SITUACIÓN FISCAL", "CÉDULA DE IDENTIFICACIÓN FISCAL",
            "REGISTRO FEDERAL DE CONTRIBUYENTES", "RFC", "SAT", "idCIF",
        ),
        id_patterns=(r"[A-ZÑ&]{3,4}\d{6}[0-9A-Z]{3}",), fixed_format=True, deterministic=True,
        attribute_keys=("id.rfc", "entity.legal_name", "address.registered"),
    ),
    DocTypeSpec(
        "MX_COMPROBANTE_DOMICILIO", "Comprobante de Domicilio", "address", ("MX",),
        anchors_es=("COMPROBANTE DE DOMICILIO", "CFE", "COMISIÓN FEDERAL DE ELECTRICIDAD", "RECIBO"),
        deterministic=False, attribute_keys=("address.residential", "doc.issue_date"),
    ),
    DocTypeSpec(
        "MX_ACTA_CONSTITUTIVA", "Acta Constitutiva", "corporate", ("MX",), applies_to="corporate",
        anchors_es=("ACTA CONSTITUTIVA", "ESCRITURA PÚBLICA", "NOTARIO", "FOLIO MERCANTIL"),
        deterministic=False,
        attribute_keys=("entity.legal_name", "entity.incorporation_date", "ownership.director"),
    ),
    # ---- Cross-jurisdiction proof-of-address / financial (LLM-favouring) ----
    DocTypeSpec(
        "UTILITY_BILL", "Utility Bill (proof of address)", "address", ("US", "CA", "MX"),
        anchors_en=("UTILITY", "ELECTRIC", "STATEMENT OF ACCOUNT", "ACCOUNT NUMBER"),
        anchors_es=("RECIBO", "AGUA", "LUZ", "TELÉFONO"),
        attribute_keys=("address.residential", "doc.issue_date"),
    ),
    DocTypeSpec(
        "BANK_STATEMENT", "Bank Statement", "income", ("US", "CA", "MX"),
        anchors_en=("STATEMENT", "ACCOUNT SUMMARY", "BEGINNING BALANCE", "IBAN"),
        anchors_es=("ESTADO DE CUENTA",),
        attribute_keys=("account.number", "account.balance", "address.mailing"),
    ),
)


DOC_TYPE_BY_CODE: dict[str, DocTypeSpec] = {d.code: d for d in DOC_TYPES}
ALL_DOC_TYPE_CODES: tuple[str, ...] = tuple(DOC_TYPE_BY_CODE)


def anchors_for(lang: str) -> dict[str, tuple[str, ...]]:
    """Return {doc_type_code: anchor_strings} for a language ('en' or 'es')."""
    out: dict[str, tuple[str, ...]] = {}
    for spec in DOC_TYPES:
        anchors = spec.anchors_es if lang == "es" else spec.anchors_en
        if anchors:
            out[spec.code] = anchors
    return out


def deterministic_doc_types() -> frozenset[str]:
    return frozenset(d.code for d in DOC_TYPES if d.deterministic)
