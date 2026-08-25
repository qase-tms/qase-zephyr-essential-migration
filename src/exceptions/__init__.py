from .api import APIError


class ImportException(Exception):
    pass


__all__ = ["APIError", "ImportException"]
