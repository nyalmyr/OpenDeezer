from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType


async def run_setup(session):
    await session.form([ConfigEntry(key="information", type=ConfigEntryType.LABEL,
        label="Musique en famille", required=False,
        description="Ouvrez /opendeezer-family/ sur ce serveur Music Assistant. Les comptes OpenDeezer déjà connectés seront disponibles. Aucun abonnement ni compte externe supplémentaire.")], step_id="family", last_step=True)
    await session.finish({})
