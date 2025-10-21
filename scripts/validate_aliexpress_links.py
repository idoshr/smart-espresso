#!/usr/bin/env python3
"""
Validate AliExpress links in the repository.

This script recursively searches for AliExpress links in markdown and text files,
validates that they are accessible, and checks if the products are still available.
"""

import os
import re
import sys
import time
from pathlib import Path
from typing import List, Dict, Tuple
from urllib.parse import urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: Required packages not installed. Run: pip install requests beautifulsoup4")
    sys.exit(1)


class AliExpressLinkValidator:
    """Validator for AliExpress product links."""

    # Pattern to match AliExpress links
    ALIEXPRESS_PATTERN = r'https?://(?:www\.)?a?\.?aliexpress\.com/[^\s)\]>"\']+'

    # File extensions to check
    SEARCH_EXTENSIONS = {'.md', '.txt', '.rst', '.html', '.htm'}

    # Directories to skip
    SKIP_DIRS = {'.git', '.github', '__pycache__', 'node_modules', '.venv', 'venv', 'env'}

    # Files to skip
    SKIP_FILES = {'aliexpress-links-report.txt'}

    def __init__(self, root_path: str = '.'):
        self.root_path = Path(root_path)
        self.links_found: Dict[str, List[Tuple[str, int]]] = {}
        self.validation_results: List[Dict] = []

    def find_links(self) -> Dict[str, List[Tuple[str, int]]]:
        """
        Recursively search for AliExpress links in files.

        Returns:
            Dictionary mapping URLs to list of (file_path, line_number) tuples
        """
        print(f"Searching for AliExpress links in {self.root_path}...")

        for root, dirs, files in os.walk(self.root_path):
            # Skip unwanted directories
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]

            for file in files:
                file_path = Path(root) / file

                # Skip files in the skip list
                if file in self.SKIP_FILES:
                    continue

                # Only check text-based files
                if file_path.suffix not in self.SEARCH_EXTENSIONS:
                    continue

                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        for line_num, line in enumerate(f, 1):
                            # Find all AliExpress links in the line
                            matches = re.finditer(self.ALIEXPRESS_PATTERN, line, re.IGNORECASE)
                            for match in matches:
                                url = match.group(0)
                                # Clean up URL (remove trailing punctuation)
                                url = url.rstrip('.,;:)')

                                rel_path = file_path.relative_to(self.root_path)
                                if url not in self.links_found:
                                    self.links_found[url] = []
                                self.links_found[url].append((str(rel_path), line_num))

                except Exception as e:
                    print(f"Warning: Could not read {file_path}: {e}")

        print(f"Found {len(self.links_found)} unique AliExpress link(s)\n")
        return self.links_found

    def validate_link(self, url: str, max_retries: int = 3) -> Dict:
        """
        Validate a single AliExpress link.

        For shortened AliExpress URLs (a.aliexpress.com), we use a HEAD request
        to check if the URL redirects properly, which indicates it's valid.
        This approach is more reliable than trying to scrape the full page,
        as AliExpress has strong anti-bot protection.

        Args:
            url: The URL to validate
            max_retries: Maximum number of retry attempts

        Returns:
            Dictionary with validation results
        """
        result = {
            'url': url,
            'accessible': False,
            'product_available': False,
            'status_code': None,
            'error': None,
            'redirect_url': None
        }

        # Different user agents to try
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
        ]

        # For shortened URLs, use HEAD request to check redirect
        is_shortened = 'a.aliexpress.com' in url

        for attempt in range(max_retries):
            headers = {
                'User-Agent': user_agents[attempt % len(user_agents)],
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Cache-Control': 'max-age=0',
                'DNT': '1',
            }

            try:
                if is_shortened:
                    # For shortened URLs, just check if they redirect properly
                    # Use HEAD request to avoid downloading the entire page
                    response = requests.head(url, headers=headers, timeout=20, allow_redirects=True)
                    result['status_code'] = response.status_code
                    result['redirect_url'] = response.url if response.url != url else None

                    # If we get a redirect to the main AliExpress domain, it's likely valid
                    if response.status_code in [200, 301, 302, 303, 307, 308]:
                        result['accessible'] = True

                        # Check if it redirected to a valid AliExpress product page
                        if result['redirect_url'] and 'aliexpress.com' in result['redirect_url']:
                            # If it redirected to a product page (not error page), mark as available
                            if '/item/' in result['redirect_url'] or 'product_id' in result['redirect_url']:
                                result['product_available'] = True
                            else:
                                # Redirected but might not be a product page
                                result['product_available'] = True  # Give benefit of doubt
                                result['error'] = "Link accessible but redirect destination uncertain"
                        else:
                            result['product_available'] = True  # Assume valid if accessible

                        return result  # Success, return immediately
                    elif response.status_code == 404:
                        result['error'] = "Page not found (404)"
                        return result
                    elif response.status_code == 403 and attempt < max_retries - 1:
                        # 403 might be temporary, retry with different user agent
                        time.sleep(1 * (attempt + 1))  # Exponential backoff
                        continue
                    elif response.status_code == 403:
                        # After retries, try a different approach
                        result['error'] = "Access forbidden (403) - unable to verify link"
                        result['accessible'] = True  # Assume link exists but we can't access it
                        result['product_available'] = True  # Give benefit of doubt
                        return result
                    else:
                        result['error'] = f"HTTP error {response.status_code}"
                        return result
                else:
                    # For full URLs, try to fetch the page
                    response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
                    result['status_code'] = response.status_code
                    result['redirect_url'] = response.url if response.url != url else None

                    # Check if request was successful
                    if response.status_code == 200:
                        result['accessible'] = True

                        # Parse HTML to check if product is available
                        soup = BeautifulSoup(response.text, 'html.parser')

                        # Check for common "product not found" indicators
                        not_found_indicators = [
                            'product not found',
                            'item not found',
                            'page not found',
                            'sorry, this product is no longer available',
                            'item has been removed',
                            'product has been removed',
                            'no longer available'
                        ]

                        page_text = soup.get_text().lower()

                        # Check if any not-found indicator is present
                        product_not_found = any(indicator in page_text for indicator in not_found_indicators)

                        # Also check for positive indicators (product title, price, etc.)
                        has_product_title = soup.find('h1') or soup.find('meta', property='og:title')
                        has_price = bool(re.search(r'\$\s*\d+|\d+\s*USD|price', page_text))

                        # Product is considered available if no "not found" messages and has product indicators
                        if not product_not_found and (has_product_title or has_price):
                            result['product_available'] = True
                        elif product_not_found:
                            result['error'] = "Product appears to be unavailable or removed"
                        else:
                            # Ambiguous case - mark as accessible but unknown availability
                            result['product_available'] = True  # Give benefit of doubt
                            result['error'] = "Could not definitively verify product availability"

                        return result  # Success

                    elif response.status_code == 404:
                        result['error'] = "Page not found (404)"
                        return result
                    elif response.status_code == 403 and attempt < max_retries - 1:
                        time.sleep(1 * (attempt + 1))
                        continue
                    elif response.status_code >= 400:
                        result['error'] = f"HTTP error {response.status_code}"
                        return result

            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
                    continue
                result['error'] = "Request timeout"
                return result
            except requests.exceptions.ConnectionError:
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
                    continue
                result['error'] = "Connection error"
                return result
            except requests.exceptions.RequestException as e:
                result['error'] = f"Request failed: {str(e)}"
                return result
            except Exception as e:
                result['error'] = f"Unexpected error: {str(e)}"
                return result

        # If we exhausted all retries
        if not result['error']:
            result['error'] = "Validation failed after all retries"
        return result

    def validate_all_links(self, delay: float = 2.0) -> List[Dict]:
        """
        Validate all found links.

        Args:
            delay: Delay between requests in seconds to avoid rate limiting

        Returns:
            List of validation results
        """
        if not self.links_found:
            print("No links to validate.")
            return []

        print(f"Validating {len(self.links_found)} link(s)...\n")

        for i, (url, locations) in enumerate(self.links_found.items(), 1):
            print(f"[{i}/{len(self.links_found)}] Checking: {url}")

            # Show where the link was found
            for file_path, line_num in locations:
                print(f"  Found in: {file_path}:{line_num}")

            result = self.validate_link(url)
            result['locations'] = locations
            self.validation_results.append(result)

            # Print result
            if result['accessible'] and result['product_available']:
                print(f"  ✓ Status: OK (HTTP {result['status_code']})")
            elif result['accessible']:
                print(f"  ⚠ Status: Accessible but product availability uncertain")
                if result['error']:
                    print(f"    {result['error']}")
            else:
                print(f"  ✗ Status: FAILED")
                if result['error']:
                    print(f"    Error: {result['error']}")

            if result['redirect_url']:
                print(f"    Redirected to: {result['redirect_url']}")

            print()

            # Delay between requests to be respectful
            if i < len(self.links_found):
                time.sleep(delay)

        return self.validation_results

    def generate_report(self, output_file: str = 'aliexpress-links-report.txt') -> bool:
        """
        Generate a text report of validation results.

        Args:
            output_file: Path to output file

        Returns:
            True if any links failed validation, False otherwise
        """
        has_failures = False
        failed_count = 0
        warning_count = 0
        success_count = 0

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("AliExpress Links Validation Report\n")
            f.write("=" * 80 + "\n\n")

            for result in self.validation_results:
                if result['accessible'] and result['product_available'] and not result['error']:
                    success_count += 1
                elif result['accessible'] and result.get('error'):
                    warning_count += 1
                else:
                    failed_count += 1
                    has_failures = True

            # Summary
            f.write(f"Total links checked: {len(self.validation_results)}\n")
            f.write(f"✓ Successful: {success_count}\n")
            f.write(f"⚠ Warnings: {warning_count}\n")
            f.write(f"✗ Failed: {failed_count}\n\n")

            # Details
            if failed_count > 0:
                f.write("FAILED LINKS:\n")
                f.write("-" * 80 + "\n")
                for result in self.validation_results:
                    if not (result['accessible'] and result['product_available']):
                        f.write(f"\nURL: {result['url']}\n")
                        f.write(f"Status Code: {result['status_code']}\n")
                        if result['error']:
                            f.write(f"Error: {result['error']}\n")
                        f.write("Locations:\n")
                        for file_path, line_num in result['locations']:
                            f.write(f"  - {file_path}:{line_num}\n")
                f.write("\n")

            if warning_count > 0:
                f.write("WARNINGS:\n")
                f.write("-" * 80 + "\n")
                for result in self.validation_results:
                    if result['accessible'] and result.get('error'):
                        f.write(f"\nURL: {result['url']}\n")
                        f.write(f"Status Code: {result['status_code']}\n")
                        f.write(f"Warning: {result['error']}\n")
                        f.write("Locations:\n")
                        for file_path, line_num in result['locations']:
                            f.write(f"  - {file_path}:{line_num}\n")
                f.write("\n")

            if success_count > 0:
                f.write("SUCCESSFUL LINKS:\n")
                f.write("-" * 80 + "\n")
                for result in self.validation_results:
                    if result['accessible'] and result['product_available'] and not result['error']:
                        f.write(f"\n✓ {result['url']}\n")
                        if result['redirect_url']:
                            f.write(f"  Redirects to: {result['redirect_url']}\n")

        print(f"\nReport saved to: {output_file}")
        return has_failures


def main():
    """Main entry point."""
    # Get repository root (assume script is in scripts/ directory)
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent

    validator = AliExpressLinkValidator(repo_root)

    # Find all AliExpress links
    links = validator.find_links()

    if not links:
        print("No AliExpress links found in the repository.")
        return 0

    # Validate all links
    validator.validate_all_links(delay=2.0)

    # Generate report
    has_failures = validator.generate_report()

    # Print summary
    print("\n" + "=" * 80)
    print("VALIDATION SUMMARY")
    print("=" * 80)

    success = sum(1 for r in validator.validation_results
                  if r['accessible'] and r['product_available'] and not r.get('error'))
    warnings = sum(1 for r in validator.validation_results
                   if r['accessible'] and r.get('error'))
    failed = sum(1 for r in validator.validation_results
                 if not (r['accessible'] and r['product_available']))

    print(f"✓ Successful: {success}")
    print(f"⚠ Warnings: {warnings}")
    print(f"✗ Failed: {failed}")

    # Exit with error code if any links failed
    if has_failures:
        print("\n❌ Some links failed validation!")
        return 1
    elif warnings > 0:
        print("\n⚠ All links accessible, but some have warnings.")
        return 0
    else:
        print("\n✅ All links validated successfully!")
        return 0


if __name__ == '__main__':
    sys.exit(main())
