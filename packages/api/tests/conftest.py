import os

import pytest
from fastapi.testclient import TestClient

os.environ["APP_ENV"] = "test"

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
