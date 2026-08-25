from ...service.qase import QaseService
from ...service.zephyr_essential import ZephyrEssentialService
from ...support.logger import Logger
from ...support.mappings import Mappings
from ...support.pools import Pools


class Attachments:
    """Attachment handling for Zephyr Essential.

    Zephyr Essential Cloud does not expose a bulk attachment download API for external
    clients. Attachments linked to test cases are accessible only through the
    Zephyr Essential UI or via Jira attachments. The actual upload/link of attachments
    happens during case import when the Qase API supports it.

    This class is kept as a no-op stub consistent with other migration scripts so
    the Importer can call it unconditionally.
    """

    def __init__(
        self,
        qase_service: QaseService,
        source_service: ZephyrEssentialService,
        logger: Logger,
        mappings: Mappings,
        config,
        pools: Pools,
    ):
        self.qase = qase_service
        self.zephyr = source_service
        self.logger = logger
        self.mappings = mappings
        self.config = config
        self.pools = pools

    def import_all_attachments(self) -> Mappings:
        self.logger.log(
            "[Attachments] Zephyr Essential attachment download is not supported via the public API; "
            "skipping pre-import phase."
        )
        return self.mappings
