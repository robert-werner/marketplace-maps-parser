import asyncio
import httpx


async def main() -> None:
    endpoint = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
    )

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30 ,
    ) as client:
        response = await client.get(
            endpoint,
            params={
                "url": (
                    "/product/"
                    "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
                    "/reviews"
                ),
            },
        )
        print(response.request)

        print(response.status_code)
        print(response.text[:1000])


if __name__ == "__main__":
    asyncio.run(main())