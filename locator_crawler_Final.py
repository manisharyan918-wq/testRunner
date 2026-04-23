import subprocess
from playwright.sync_api import sync_playwright
import os
import argparse
import re

def extract_locators_from_page(page):
    elements = page.query_selector_all("a, button, input, select, textarea, label")
    seen = set()
    result = []

    for i, el in enumerate(elements):
        try:
            if not el.is_visible():
                continue

            tag = el.evaluate("el => el.tagName.toLowerCase()")
            id_attr = el.get_attribute("id")
            name_attr = el.get_attribute("name")
            aria = el.get_attribute("aria-label")
            title = el.get_attribute("title")
            href_attr = el.get_attribute("href")
            placeholder = el.get_attribute("placeholder")
            text = el.inner_text().strip()

            selector = None
            if id_attr and id_attr.isidentifier():
                selector = f"#{id_attr}"
            elif href_attr and tag == "a":
                selector = f"a[href='{href_attr}']"
            elif name_attr:
                selector = f"{tag}[name='{name_attr}']"
            elif aria:
                selector = f"{tag}[aria-label='{aria}']"
            elif title:
                selector = f"{tag}[title='{title}']"
            elif placeholder:
                selector = f"{tag}[placeholder='{placeholder}']"
            elif text and len(text) < 40:
                safe = text.replace('"', '\\"')
                selector = f"{tag}:has-text(\"{safe}\")"

            if selector and selector not in seen:
                seen.add(selector)
                result.append({
                    "tag": tag,
                    "text": text,
                    "id": id_attr,
                    "name": name_attr,
                    "aria": aria,
                    "title": title,
                    "placeholder": placeholder,
                    "selector": selector,
                    "clickable": tag in ["a", "button"] or (tag == "input" and (el.get_attribute("type") in ["submit", "button"]))
                })

        except Exception as e:
            print(f"[{i}] ⚠️ Error in fallback element: {e}")
            continue

    return result[:100]


def to_camel_case(s):
    parts = re.sub(r"[^a-zA-Z0-9 ]+", "", s).strip().split()
    if not parts:
        return "element"
    return parts[0].lower() + ''.join(word.capitalize() for word in parts[1:])

def generate_var_name(tag, attrs, text, fallback_index):
    label = (
        attrs.get("aria-label")
        or attrs.get("title")
        or attrs.get("placeholder")
        or text
        or f"{tag}_{fallback_index}"
    )

    label = re.sub(r"[^a-zA-Z0-9 ]+", "", label).strip()
    if not label:
        return f"{tag}_{fallback_index}"

    parts = label.lower().split()
    if not parts:
        return f"{tag}_{fallback_index}"

    camel = parts[0] + ''.join(word.capitalize() for word in parts[1:])

    suffix = {
        "a": "Link",
        "button": "Button",
        "input": "Field",
        "textarea": "Textarea",
        "select": "Dropdown",
        "label": "Label",
    }.get(tag, "Element")

    return camel + suffix


def extract_locators_from_url(url):
    print(f"🌐 Launching browser for {url}")
    selectors = []
    seen_selectors = set()
    used_names = set()

    def generate_var_name(tag, attrs, text, index):
        if attrs.get("aria-label"):
            base = attrs["aria-label"]
        elif attrs.get("title"):
            base = attrs["title"]
        elif attrs.get("placeholder"):
            base = attrs["placeholder"]
        elif attrs.get("name"):
            base = attrs["name"]
        elif text:
            base = text
        else:
            base = f"{tag}_{index}"

        base = ''.join(c for c in base.title() if c.isalnum())
        if not base:
            base = f"{tag}_{index}"

        suffix = "Link" if tag == "a" else "Button" if tag == "button" else "Field" if tag == "input" else ""
        var_name = f"{base}{suffix[0].upper() + suffix[1:]}" if suffix else base

        original_name = var_name
        counter = 1
        while var_name in used_names:
            var_name = f"{original_name}{counter}"
            counter += 1
        used_names.add(var_name)

        return var_name

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="load", timeout=90000)
        page.wait_for_timeout(5000)  # Let the DOM settle fully

        elements = page.query_selector_all("a, button, input, select, textarea, label, div[role=checkbox]")

        for i, el in enumerate(elements):
            try:
                if not el.is_visible():
                    continue

                tag = el.evaluate("el => el.tagName.toLowerCase()")
                id_attr = el.get_attribute("id")
                name_attr = el.get_attribute("name")
                aria = el.get_attribute("aria-label")
                title = el.get_attribute("title")
                placeholder = el.get_attribute("placeholder")
                text = el.inner_text().strip()
                text_content = el.evaluate("el => el.textContent").strip()

                selector = None
                if id_attr and id_attr.isidentifier():
                    selector = f"{tag}#{id_attr}"
                elif name_attr:
                    selector = f"{tag}[name='{name_attr}']"
                elif aria:
                    selector = f"{tag}[aria-label='{aria}']"
                elif title:
                    selector = f"{tag}[title='{title}']"
                elif placeholder:
                    selector = f"{tag}[placeholder='{placeholder}']"
                elif text and len(text) < 40:
                    safe_text = text.replace('"', '\\"')
                    selector = f"{tag}:has-text(\"{safe_text}\")"

                if selector and selector not in seen_selectors:
                    seen_selectors.add(selector)

                    var_name = generate_var_name(tag, {
                        "id": id_attr,
                        "name": name_attr,
                        "aria-label": aria,
                        "title": title,
                        "placeholder": placeholder
                    }, text, i)

                    selectors.append({
                        "tag": tag,
                        "selector": selector,
                        "text": text,
                        "textContent": text_content[:100],
                        "clickable": tag in ["a", "button", "input", "div"],
                        "id": id_attr,
                        "name": name_attr,
                        "ariaLabel": aria,
                        "title": title,
                        "placeholder": placeholder,
                        "var_name": var_name
                    })

                    # print(f"[{i}] ✅ {var_name} => {selector}") ----- check for selectors print here

            except Exception as e:
                print(f"[{i}] ⚠️ Skipped due to error: {e}")
                continue

        browser.close()
        return selectors
    
    # ✅ Sort selectors by var_name for consistent order
    selectors.sort(key=lambda s: s[4])

    # ✅ Write Playwright-style Java file
    playwright_path = "src/main/java/locators/PageLocatorsPlaywright.java"
    os.makedirs(os.path.dirname(playwright_path), exist_ok=True)
    with open(playwright_path, "w", encoding="utf-8") as f:
        f.write("package locators;\n\n")
        f.write("public class PageLocatorsPlaywright {\n")
        for tag, selector, attrs, text, var_name in selectors:
            f.write(f'    public static final String {var_name} = "{selector}";\n')
        f.write("}\n")

    print(f"\n✅ Final count: {len(selectors)} locators written to PageLocatorsPlaywright.java")
    return selectors


def convert_playwright_to_selenium(playwright_path="src/main/java/locators/PageLocatorsPlaywright.java",
                                   selenium_path="src/main/java/locators/PageLocatorsSelenium.java"):
    if not os.path.exists(playwright_path):
        print("❌ Playwright locators file not found.")
        return

    print("🔁 Converting Playwright locators to Selenium...")

    selenium_lines = [
        "package locators;\n",
        "import org.openqa.selenium.By;\n",
        "import org.openqa.selenium.WebElement;\n",
        "import org.openqa.selenium.support.FindBy;\n\n",
        "public class PageLocatorsSelenium {\n"
    ]

    with open(playwright_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        if line.startswith("public static final String"):
            try:
                parts = line.split(" = ")
                var_name = parts[0].split()[-1]  # e.g., searchBox
                selector_raw = parts[1].strip('";')  # e.g., a[aria-label="Search"]

                # Convert to Selenium-friendly CSS or XPath
                if selector_raw.startswith("#") or "[" in selector_raw:
                    # Assume CSS Selector
                    converted = f'    public static final By {var_name} = By.cssSelector("{selector_raw}");'
                elif ":has-text(" in selector_raw:
                    # Convert :has-text() → XPath
                    tag, text = selector_raw.split(":has-text(")
                    clean_text = text.strip('")')
                    xpath = f"//{tag}[normalize-space(text())='{clean_text}']"
                    converted = f'    public static final By {var_name} = By.xpath("{xpath}");'
                else:
                    continue  # Skip unsupported patterns

                selenium_lines.append(converted + "\n")
            except Exception as e:
                print(f"⚠️ Skipped line due to parse error: {line}")

    selenium_lines.append("}\n")

    # Write to Selenium file
    os.makedirs(os.path.dirname(selenium_path), exist_ok=True)
    with open(selenium_path, "w", encoding="utf-8") as f:
        f.writelines(selenium_lines)

    print(f"✅ Selenium locators written to: {selenium_path}")

def generate_var_name(tag, attrs, text, index):
    if text and len(text) < 30:
        base = text.strip().lower().replace(" ", "_").replace("-", "_")
    elif attrs.get("name"):
        base = attrs["name"].strip().lower().replace(" ", "_").replace("-", "_")
    elif attrs.get("id"):
        base = attrs["id"].strip().lower().replace(" ", "_").replace("-", "_")
    else:
        base = f"{tag}_{index}"
    # Ensure valid Java var name
    base = re.sub(r'\W+', '_', base)
    if base[0].isdigit():
        base = "_" + base
    return base


def main():
    parser = argparse.ArgumentParser(description="🕷️ Locator Crawler Utility")
    parser.add_argument("--url", required=True, help="Target URL to crawl")
    # parser.add_argument("--feature", required=True, help="Path to .feature file")
    args = parser.parse_args()

    # Step 2: Extract locators using Playwright and convert to CSS/XPath for Selenium
    locators = extract_locators_from_url(args.url)

    convert_playwright_to_selenium()

    # Step 3: Write them to PageLocators.java in correct format
#write_java_locators(locators)

    print("Locator crawling complete!")

if __name__ == "__main__":
    main()
