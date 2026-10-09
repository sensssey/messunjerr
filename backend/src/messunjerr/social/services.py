"""Службы контекста social, создаваемые один раз при старте процесса.

Социальный граф выше профилей в графе контекстов (4.2), поэтому профили знают его только через
порты: `Relationships` (как зритель связан с человеком), `ProfileCounters` (друзья, подписчики,
подписки), `MeCountersSource` (заявки и запросы в шапке клиента) и `ProfileVisibilityListener`
(открытие профиля одобряет ждущие запросы на подписку). Эти реализации передаёт в профили корень
приложения (`messunjerr.main`).
"""

from dataclasses import dataclass

from messunjerr.social.commands.follows import OpenedProfileApprovals
from messunjerr.social.queries.counters import SocialCounters, SocialMeCounters
from messunjerr.social.queries.relationships import SocialRelationships


@dataclass(frozen=True, slots=True)
class SocialServices:
    relationships: SocialRelationships
    counters: SocialCounters
    me_counters: SocialMeCounters
    visibility: OpenedProfileApprovals


def create_social_services() -> SocialServices:
    return SocialServices(
        relationships=SocialRelationships(),
        counters=SocialCounters(),
        me_counters=SocialMeCounters(),
        visibility=OpenedProfileApprovals(),
    )
