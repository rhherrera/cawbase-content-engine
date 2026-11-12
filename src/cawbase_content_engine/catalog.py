"""Starter catalog of sensitive types (A02, D-A02-03 / E-A02-06).

A packaged, versioned *template*: creating a profile copies these entries into it (with provenance);
the catalog is never a shared, editable table. Updating this module in a later release does not touch
existing profiles or their published versions. The entries are detection *definitions*, not policies,
they are not exhaustive and they may overlap; they promise no detection quality. All examples are
synthetic (reserved domains, obviously fake values).

Pure data, no I/O. Not imported by ``cawbase_content_engine.__init__`` (loaded lazily).
"""

from __future__ import annotations

from typing import Any

from .models import Examples, StarterCatalog, StarterCatalogEntry

CATALOG_ID = "cce-starter-sensitive-types"
CATALOG_VERSION = 1

# entry -> (names, definitions, examples) with "es" / "en" variants.
_RAW: dict[str, dict[str, Any]] = {
    "personal_data": {
        "names": {"es": "Datos personales", "en": "Personal data"},
        "definitions": {
            "es": (
                "Información que identifica o permite identificar a una persona física, sola o combinada: nombre y "
                "apellidos, correo electrónico, teléfono, dirección postal, fecha de nacimiento, imagen o datos de "
                "ubicación personal. Excluye los números de documentos oficiales (ver government_id), las credenciales "
                "(password, api_key_secret) y los datos económicos (salary_compensation, financial_information)."
            ),
            "en": (
                "Information that identifies, or makes identifiable, a natural person alone or in combination: full "
                "name, email address, phone number, postal address, date of birth, image or personal location data. "
                "Excludes official document numbers (see government_id), credentials (password, api_key_secret) and "
                "economic data (salary_compensation, financial_information)."
            ),
        },
        "examples": {
            "es": (
                ["Contactar a Ana Ejemplo (ana.ejemplo@example.test), tel. +00 555 0100."],
                ["Escribir a soporte@example.test para dudas generales."],
            ),
            "en": (
                ["Contact Alex Sample at alex.sample@example.test, phone +00 555 0100."],
                ["Write to support@example.test for general questions."],
            ),
        },
    },
    "government_id": {
        "names": {"es": "Identificadores oficiales", "en": "Official identifiers"},
        "definitions": {
            "es": (
                "Números o códigos asignados por una autoridad a una persona o entidad: documento nacional de "
                "identidad, pasaporte, número fiscal, seguridad social, licencia de conducir. Distinto de "
                "personal_data (atributos descriptivos o de contacto) y de financial_information (cuentas y tarjetas)."
            ),
            "en": (
                "Numbers or codes assigned by an authority to a person or entity: national identity document, "
                "passport, tax number, social security number, driver's licence. Different from personal_data "
                "(descriptive or contact attributes) and from financial_information (accounts and cards)."
            ),
        },
        "examples": {
            "es": (["Documento de identidad: 00000000-X (ejemplo)."], ["Pedido número 00000000 del catálogo."]),
            "en": (["Passport number: X0000000 (sample)."], ["Order number 00000000 in the catalog."]),
        },
    },
    "salary_compensation": {
        "names": {"es": "Salarios y compensación", "en": "Salary and compensation"},
        "definitions": {
            "es": (
                "Remuneración individual vinculada a una persona o puesto identificable: salario, bonos, ajustes, "
                "comisiones. No incluye bandas salariales o cifras agregadas sin persona ni puesto identificable. "
                "Las cuentas y los saldos son financial_information."
            ),
            "en": (
                "Individual pay linked to an identifiable person or position: salary, bonuses, adjustments, "
                "commissions. Does not include salary bands or aggregate figures without an identifiable person or "
                "position. Accounts and balances are financial_information."
            ),
        },
        "examples": {
            "es": (
                ["El salario bruto anual de Ana Ejemplo es de 00.000 EUR."],
                ["Las bandas salariales del sector se publican cada año."],
            ),
            "en": (
                ["Alex Sample's gross annual salary is 00,000 USD."],
                ["Industry salary bands are published yearly."],
            ),
        },
    },
    "financial_information": {
        "names": {"es": "Información financiera", "en": "Financial information"},
        "definitions": {
            "es": (
                "Datos de instrumentos y posición financiera de una persona o entidad: números de cuenta (IBAN u "
                "otros), tarjetas, saldos, movimientos, deudas. Los pagos de salario son salary_compensation y los "
                "números de documentos son government_id. Una cuenta o tarjeta es financiera aunque aparezca junto a "
                "datos personales."
            ),
            "en": (
                "Data about the financial instruments and position of a person or entity: account numbers (IBAN or "
                "others), cards, balances, transactions, debts. Salary payments are salary_compensation and document "
                "numbers are government_id. An account or card is financial even when it appears next to personal data."
            ),
        },
        "examples": {
            "es": (
                ["Cuenta ES00 0000 0000 0000 0000 0000, saldo 0.000,00 EUR."],
                ["La entidad ofrece cuentas de ahorro."],
            ),
            "en": (
                ["Card number 0000 0000 0000 0000, balance 0,000.00 USD."],
                ["The bank offers savings accounts."],
            ),
        },
    },
    "password": {
        "names": {"es": "Contraseñas", "en": "Passwords"},
        "definitions": {
            "es": (
                "Contraseñas, PIN, frases de paso y respuestas secretas usadas para autenticarse, las haya elegido "
                "una persona o generado una máquina (una contraseña generada por un gestor sigue siendo password). La "
                "distinción con api_key_secret se basa en la función y el contexto observable (una credencial de "
                "inicio de sesión frente a una clave o token técnico de acceso a un servicio o API), "
                "no en quién generó "
                "el secreto; no hace falta deducir su procedencia. Un mismo fragmento puede encajar en ambos tipos."
            ),
            "en": (
                "Passwords, PINs, passphrases and secret answers used to authenticate, whether chosen by a person or "
                "generated by a machine (a password produced by a password manager is still a password). The boundary "
                "with api_key_secret is function and observable context (a login credential versus a technical key or "
                "token for accessing a service or API), not who generated the secret; its origin need not be inferred. "
                "The same fragment may fit both types."
            ),
        },
        "examples": {
            "es": (
                ["Mi contraseña es EjemploNoReal-123."],
                ["La contraseña debe tener al menos ocho caracteres."],
            ),
            "en": (
                ["My password is NotARealPassword-123."],
                ["The password must be at least eight characters."],
            ),
        },
    },
    "api_key_secret": {
        "names": {"es": "API keys y tokens", "en": "API keys and tokens"},
        "definitions": {
            "es": (
                "Claves de API, tokens de acceso o refresco y otros secretos técnicos de servicio o aplicación (por "
                "ejemplo claves privadas o cadenas de conexión con credenciales). Las contraseñas, PIN, frases de paso "
                "y respuestas secretas de autenticación son password, sin importar su origen. Una cadena de conexión "
                "que incluya una contraseña puede encajar en ambos tipos."
            ),
            "en": (
                "API keys, access or refresh tokens and other technical service or application secrets (for example "
                "private keys or connection strings with credentials). Passwords, PINs, passphrases and secret "
                "authentication answers are password, regardless of their origin. A connection string that includes a "
                "password may fit both types."
            ),
        },
        "examples": {
            "es": (
                ["Authorization: Bearer EJEMPLO_TOKEN_NO_REAL_0000"],
                ["Use un token en el encabezado Authorization."],
            ),
            "en": (
                ["api_key = EXAMPLE_NOT_A_REAL_KEY_0000"],
                ["Send the token in the Authorization header."],
            ),
        },
    },
}


def starter_catalog() -> StarterCatalog:
    """The packaged catalog (pure data; no I/O, no permissions, belongs to no scope)."""
    entries = []
    for key, raw in _RAW.items():
        examples = {
            lang: Examples(positive=tuple(pos), negative=tuple(neg)) for lang, (pos, neg) in raw["examples"].items()
        }
        entries.append(
            StarterCatalogEntry(
                entry=key,
                names=dict(raw["names"]),
                definitions=dict(raw["definitions"]),
                examples=examples,
            )
        )
    return StarterCatalog(catalog_id=CATALOG_ID, catalog_version=CATALOG_VERSION, entries=tuple(entries))
