#!/usr/bin/env python3
"""
Browser-driven aninamu.com strategy, using Playwright.

Simulates clicking the 'Watch Now' button to reach the player page and reports
the media URLs it finds. Requires the optional ``playwright`` dependency, so
this module is never imported by the source registry.
"""

import sys
import time

from playwright.sync_api import sync_playwright


def download_aninamu_video(page_url: str, output_path: str) -> None:
    """
    Download video from aninamu page by simulating button click.

    :param page_url: The URL of the aninamu episode page
    :param output_path: Path where to save the video
    :raises Exception: If video cannot be downloaded
    """
    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(headless=False)  # Set to False to see what's happening
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        # Navigate to the page
        print(f"Navigating to: {page_url}")
        page.goto(page_url, timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)

        # Find and click the "바로 보기" button
        print("Looking for '바로 보기' button...")
        watch_button = page.locator('input[type="submit"][value="바로 보기"]')

        if watch_button.count() == 0:
            print("Could not find '바로 보기' button")
            browser.close()
            return

        # Get the form action and video URL before clicking
        form = page.locator('form:has(input[value="바로 보기"])').first
        form_html = form.inner_html()
        print("\nForm content:")
        print(form_html[:500])

        # Extract video URL from the form
        video_url_input = page.locator('input[name="vurl"]')
        if video_url_input.count() > 0:
            video_url = video_url_input.get_attribute("value")
            print(f"\nFound video URL: {video_url}")

        # Click the button - this will open a new tab
        print("\nClicking '바로 보기' button...")
        with context.expect_page() as new_page_info:
            watch_button.click()

        new_page = new_page_info.value
        print(f"New page opened: {new_page.url}")

        # Wait for the new page to load
        new_page.wait_for_load_state("networkidle")
        time.sleep(2)

        # Look for video element or iframe
        print("\nLooking for video sources in the new page...")

        # Check for iframes
        iframes = new_page.locator("iframe")
        if iframes.count() > 0:
            print(f"Found {iframes.count()} iframe(s)")
            for i in range(iframes.count()):
                iframe_src = iframes.nth(i).get_attribute("src")
                print(f"  Iframe {i + 1} src: {iframe_src}")

        # Check for video elements
        videos = new_page.locator("video")
        if videos.count() > 0:
            print(f"Found {videos.count()} video element(s)")
            for i in range(videos.count()):
                video = videos.nth(i)
                src = video.get_attribute("src")
                print(f"  Video {i + 1} src: {src}")

                # Check for source elements within video
                sources = video.locator("source")
                for j in range(sources.count()):
                    source_src = sources.nth(j).get_attribute("src")
                    source_type = sources.nth(j).get_attribute("type")
                    print(f"    Source {j + 1}: {source_src} (type: {source_type})")

        # Get page HTML to analyze
        html_content = new_page.content()
        if ".m3u8" in html_content:
            print("\nPage contains .m3u8 references (HLS streaming)")
            # Extract m3u8 URLs
            import re

            m3u8_urls = re.findall(r'https?://[^\s<>"]+\.m3u8[^\s<>"]*', html_content)
            for url in m3u8_urls:
                print(f"  Found m3u8: {url}")

        if ".mp4" in html_content:
            print("\nPage contains .mp4 references")
            # Extract mp4 URLs
            import re

            mp4_urls = re.findall(r'https?://[^\s<>"]+\.mp4[^\s<>"]*', html_content)
            for url in mp4_urls:
                print(f"  Found mp4: {url}")

        # Keep browser open for inspection
        print("\n" + "=" * 60)
        print("Browser will stay open for 30 seconds so you can inspect.")
        print("Check the Network tab in browser dev tools to see video requests.")
        print("=" * 60)
        time.sleep(30)

        browser.close()


def main():
    """Main function."""
    if len(sys.argv) < 2:
        print("Usage: python -m video_migrator.sources.aninamu_playwright <aninamu_url> [output_filename]")
        print("Example: python -m video_migrator.sources.aninamu_playwright 'https://aninamu.com/...' video.mp4")
        sys.exit(1)

    page_url = sys.argv[1]
    output_filename = sys.argv[2] if len(sys.argv) > 2 else "video.mp4"

    try:
        download_aninamu_video(page_url, output_filename)
    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
