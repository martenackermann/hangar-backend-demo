from httpx import AsyncClient


async def test_health_reports_database_and_job_queue(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-request-id"]
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["environment"] == "test"
    assert body["checks"]["database"]["status"] == "ok"
    assert isinstance(body["checks"]["database"]["latencyMs"], float)
    queue = body["checks"]["jobQueue"]
    assert queue["status"] in {"ok", "degraded"}
    assert queue["pending"] >= 0
    # No worker runs during tests -> degraded, but still HTTP 200.
    assert (queue["workers"] == 0) == (queue["status"] == "degraded")


async def test_request_id_is_propagated(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"
