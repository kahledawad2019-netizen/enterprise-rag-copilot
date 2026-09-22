from __future__ import annotations

import pytest

from enterprise_copilot.text_to_sql.vanna_cloud_provider import (
    VannaCloudTextToSQLProvider,
)


class _UnavailableCloud:
    def get_training_data(self):
        raise TimeoutError("provider did not answer")


def test_remote_training_check_fails_closed_instead_of_reuploading() -> None:
    provider = object.__new__(VannaCloudTextToSQLProvider)
    provider._vanna = _UnavailableCloud()

    with pytest.raises(RuntimeError, match="refusing to upload"):
        provider._remote_training_rows()
