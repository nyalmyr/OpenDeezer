"""Family account enrollment through the authenticated MA settings UI."""
import asyncio
import os
from pathlib import Path
from aiohttp import ClientError, ClientTimeout
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from . import config_entries


class EnrollmentError(Exception):
    """Safe message without upstream response or credentials."""


async def enroll(session, label, arl):
    # Never send the admin token to a URL supplied by the setup form.
    base = os.environ.get("OPENDEEZER_ENROLL_URL", "http://127.0.0.1:8731").rstrip("/")
    token_file = os.environ.get("OPENDEEZER_ADMIN_TOKEN_FILE", "/run/secrets/opendeezer-admin")
    try:
        token = (await asyncio.to_thread(Path(token_file).read_text)).strip()
        async with session.mass.http_session.post(
            f"{base}/admin/enroll", json={"label": label, "arl": arl},
            headers={"Authorization": f"Bearer {token}"},
            timeout=ClientTimeout(total=60), allow_redirects=False,
        ) as resp:
            data = await resp.json()
            if resp.status not in (200, 201):
                messages = {
                    "invalid_session": "Connexion Deezer refusée ou expirée. Reconnectez-vous à Deezer puis recopiez le cookie ARL.",
                    "label_in_use": "Ce prénom est déjà utilisé pour un autre compte. Choisissez un nom distinct.",
                    "enrollment_busy": "Un ajout est déjà en cours. Réessayez dans quelques instants.",
                    "profile_limit": "La limite de 20 comptes est atteinte.",
                    "invalid_input": "Indiquez un prénom et la valeur du cookie ARL, sans le nom arl= ni guillemets.",
                }
                raise EnrollmentError(messages.get(data.get("error"), "Ajout impossible. Vérifiez le service OpenDeezer et sa configuration d'administration."))
    except (OSError, ClientError, TimeoutError, ValueError):
        raise EnrollmentError("Service d'ajout de comptes injoignable ou non configuré.") from None
    for provider in session.mass.providers:
        if (provider.domain == "opendeezer_home" and
                getattr(provider, "profile_id", None) == data["id"] and
                provider.instance_id != session.context.instance_id):
            raise EnrollmentError(f"Ce compte est déjà connecté : {data['label']}. Retrouvez-le dans Fournisseurs ; inutile de l'ajouter deux fois.")
    if hasattr(session.mass, "config"):
        configs = await session.mass.config.get_provider_configs(provider_domain="opendeezer_home")
        for config in configs:
            if config.instance_id == session.context.instance_id:
                continue
            raw = session.mass.config.get("providers/" + config.instance_id) or {}
            values = session.mass.config._decrypt_values(raw.get("setup_data") or {})
            if values.get("profile_token") == data["profile_token"]:
                raise EnrollmentError("Ce compte est déjà configuré, même s'il est désactivé. Réactivez-le ou reconnectez-le dans Musique en famille.")
    return {"bridge_url": base, "profile_token": data["profile_token"], "profile_label": data["label"]}


async def run_setup(session):
    if session.context.setup_data.get("profile_token"):
        await legacy_setup(session)
        return
    errors = None
    label = ""
    while True:
        entries = [
            ConfigEntry(key="instructions", type=ConfigEntryType.LABEL, required=False,
                label="Connecter un compte Deezer familial",
                description="Sur un ordinateur, connectez-vous à deezer.com avec le compte à ajouter. Ouvrez F12 → Application (Chrome/Edge) ou Stockage (Firefox) → Cookies → deezer.com. Copiez uniquement la valeur du cookie nommé arl dans le champ privé ci-dessous. Ce cookie donne accès au compte : ne le partagez jamais dans un message. Aucun mot de passe Deezer n'est demandé."),
            ConfigEntry(key="profile_label", type=ConfigEntryType.STRING,
                label="Prénom / nom du membre", value=label,
                description="Exemple : Alice. Les playlists seront préfixées par ce nom."),
            ConfigEntry(key="deezer_arl", type=ConfigEntryType.SECURE_STRING,
                label="Cookie de connexion Deezer (ARL)",
                description="Champ confidentiel. Le compte sera vérifié avant son enregistrement."),
        ]
        submitted = await session.form(entries, step_id="add_account", errors=errors, last_step=True)
        label = str(submitted.get("profile_label") or "").strip()
        arl = str(submitted.pop("deezer_arl", "") or "").strip()
        try:
            values = await enroll(session, label, arl)
            arl = ""
            await session.finish(values)
            return
        except EnrollmentError as exc:
            errors = {"base": str(exc)}
        except SetupFlowError:
            errors = {"base": "Le compte est enregistré mais Music Assistant n'a pas pu le charger. Réessayez avec le même compte."}
        finally:
            arl = ""


async def legacy_setup(session):
    values = dict(session.context.setup_data)
    errors = None
    while True:
        entries = list(config_entries())
        entries[0].value = values.get("bridge_url", "http://127.0.0.1:8731")
        entries[1].required = False
        entries.insert(0, ConfigEntry(key="account_info", type=ConfigEntryType.LABEL,
            required=False, label="Compte déjà connecté",
            description="Pour ajouter une autre personne : Fournisseurs → Ajouter → OpenDeezer Home. Laissez la clé vide pour conserver ce compte."))
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        for key, value in submitted.items():
            if key == "profile_token" and not value:
                continue  # empty secret field during reconfigure retains the current secret
            values[key] = value
        try:
            await session.finish(values)
            return
        except SetupFlowError:
            errors = {"base": "Connexion OpenDeezer impossible. Vérifier l'adresse et la clé du profil."}
