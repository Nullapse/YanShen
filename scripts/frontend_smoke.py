from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:5169"
OUTPUT = Path(__file__).resolve().parents[1] / ".visual-qa"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def assert_layout(page, route: str, errors: list[str]) -> None:
    page.goto(f"{BASE_URL}{route}", wait_until="networkidle")
    page.wait_for_timeout(300)
    metrics = page.evaluate(
        """() => ({
          overflow: (document.body.scrollWidth
            * (Number.parseFloat(document.documentElement.style.zoom) || 1))
            - document.documentElement.clientWidth,
          moduleScript: Boolean(
            document.querySelector('script[type="module"][src^="/static/app.js"]')
          ),
          mounted: Boolean(window.__gongkaoPageAbortController),
          styleSheets: document.styleSheets.length,
        })"""
    )
    assert metrics["overflow"] <= 2, (route, metrics)
    assert metrics["moduleScript"], route
    assert metrics["mounted"], (route, metrics, errors)
    assert metrics["styleSheets"] >= 1, route


def main() -> None:
    OUTPUT.mkdir(exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(CHROME),
        )
        page = browser.new_page(viewport={"width": 1220, "height": 820})
        page.on(
            "console",
            lambda message: (
                errors.append(f"console: {message.text}")
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))

        for route in ("/home", "/papers", "/", "/attempts", "/agent", "/settings"):
            assert_layout(page, route, errors)

        page.goto(f"{BASE_URL}/papers?region=%E6%B5%99%E6%B1%9F", wait_until="networkidle")
        saved_filters = page.evaluate(
            """() => Object.fromEntries(
              Array.from({ length: localStorage.length }, (_, index) =>
                localStorage.key(index)
              )
                .filter((key) => key && key.includes("filters:papers"))
                .map((key) => [key, localStorage.getItem(key)])
            )"""
        )
        assert saved_filters, (saved_filters, errors)
        page.locator(".sidebar .nav a[href='/attempts']").click()
        page.wait_for_url("**/attempts")
        page.locator(".sidebar .nav a[href='/papers']").click()
        page.wait_for_timeout(1200)
        assert "/papers" in page.url, page.url
        assert "region=%E6%B5%99%E6%B1%9F" in page.url, (page.url, saved_filters)

        core_response = page.request.get(f"{BASE_URL}/static/js/core.js")
        assert core_response.status == 200
        traversal_response = page.request.get(
            f"{BASE_URL}/static/../gongkao/db.py"
        )
        assert traversal_response.status == 404

        for width, height, name in (
            (1220, 820, "window"),
            (1920, 1080, "1080p"),
            (2560, 1440, "2k"),
        ):
            page.set_viewport_size({"width": width, "height": height})
            assert_layout(page, "/settings", errors)
            page.screenshot(
                path=OUTPUT / f"refactor-settings-{name}.png",
                full_page=True,
            )

        browser.close()

    assert not errors, "\n".join(errors)


if __name__ == "__main__":
    main()
