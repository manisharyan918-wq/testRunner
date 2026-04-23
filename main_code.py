import os
import re
import json
import hashlib
from openai import OpenAI
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError
import time
from openai.types import CompletionUsage
from openai._exceptions import RateLimitError
from locator_crawler_Final import extract_locators_from_page
import difflib
import argparse
import shutil
import sys
from io import BytesIO
import zipfile
import uuid
from pathlib import Path
import glob
import httpx

BROWSER_TYPE = os.getenv("BROWSER_TYPE", "chromium")
# Load previously generated functions
if os.path.exists("generated_steps.py"):
    with open("generated_steps.py") as f:
        try:
            exec(f.read(), globals())
            print("Loaded saved generated step handlers.")
        except Exception as e:
            print("Failed to load saved generated functions:", e)

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
# Create insecure client (skips SSL verification for corporate proxy/laptop)
insecure_http_client = httpx.Client(verify=False)
client = OpenAI(api_key=api_key, http_client=insecure_http_client)
# client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Track token usage and cost
total_prompt_tokens = 0
total_completion_tokens = 0
total_cost_usd = 0.0
total_calls = 0

# Model pricing (GPT-4o, as of July 2024)
# COST_PER_1K_PROMPT = 0.0015  # $0.0015 per 1K prompt tokens
# COST_PER_1K_COMPLETION = 0.002  # $0.002 per 1K completion tokens

COST_PER_1K_PROMPT = 0.005   # $0.005 per 1K prompt tokens for GPT-4o
COST_PER_1K_COMPLETION = 0.015  # $0.015 per 1K completion tokens for GPT-4o

intent_cache = {}
selector_cache = {}


# TEST_FILE = "test_case.txt"
TEST_FILE = None

# Check if coming from Streamlit UI (env vars)
if "TEST_FILE" in os.environ:
    TEST_FILE = os.environ["TEST_FILE"]
    # HEADLESS = os.environ.get("HEADLESS", "1") == "1"  # "1" = headless, "0" = headful
else:
    # CLI mode
    parser = argparse.ArgumentParser(description="Run AI-powered test cases from a file.")
    parser.add_argument("--test_file", help="Path to the test case file (e.g. test_case.txt)")
    # parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--headful", action="store_true", help="Run in headful (visible) mode")
    args = parser.parse_args()

    TEST_FILE = args.test_file
    # HEADLESS = args.headless or not args.headful  # Defaults to headless if not headful

    if not TEST_FILE:
        print("Error: No test file provided.")
        sys.exit(1)


def extract_locator_if_present(step):
    match = re.search(r'using locator\s+"(.+?)"', step)
    if match:
        locator = match.group(1).strip()
        if locator.startswith("/") or locator.startswith("("):
            locator = f"xpath={locator}"
        return locator
    return None

def safe_print(*args, **kwargs):
    try:
        print(*args, file=sys.stdout.buffer, **kwargs)
    except Exception:
        msg = " ".join(str(a) for a in args)
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()

def call_openai(prompt):
    global total_prompt_tokens, total_completion_tokens, total_cost_usd, total_calls

    retries = 3
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0
            )
            break  # Success, exit retry loop
        except RateLimitError as e:
            wait_time = 2 + attempt  # wait 2s, then 3s, then 4s
            safe_print(f"[RateLimitError] Attempt {attempt + 1}/{retries} — retrying in {wait_time}s...")
            time.sleep(wait_time)
    else:
        raise Exception("OpenAI rate limit exceeded even after retries.")

    usage = response.usage
    prompt_tokens = usage.prompt_tokens
    completion_tokens = usage.completion_tokens

    total_prompt_tokens += prompt_tokens
    total_completion_tokens += completion_tokens
    total_calls += 1

    cost = (prompt_tokens / 1000) * COST_PER_1K_PROMPT + (completion_tokens / 1000) * COST_PER_1K_COMPLETION
    total_cost_usd += cost

    return response.choices[0].message.content.strip()

# def get_selector_for_unsupported_step(step, page, candidates):
#     fake_intent = {
#         "action": "custom_fallback",
#         "target": step,  # let it search using raw step text
#         "value": "",
#         "field": "",
#         "action": "",
#     }

#     try:
#         selector = match_selector(step, fake_intent, candidates, page)
#         safe_print(f"Selector for fallback step: {selector}")
#         return selector
#     except Exception as e:
#         safe_print(f"Failed to get selector for fallback step: {e}")
#         return None

def handle_unknown_step(step, current_page, candidates):
    safe_print(f"\nUnrecognized step: \"{step}\" — attempting runtime fallback...")

    metadata = extract_fallback_metadata(step)
    safe_print(f"Extracted fallback metadata: {metadata}")

    # Attempt selector resolution using existing logic
    try:
        selector = match_selector(
            step,
            {
                "action": metadata.get("action", ""),
                "target": metadata.get("target_element", ""),
                "value": "",
                "field": metadata.get("field_description", ""),
                "press": "",
            },
            candidates,
            current_page
        )
        # Basic sanity check for valid selectors
        if selector and not any(bad in selector.lower() for bad in ["no ", "not found", "provided fallback", "null", "none"]):
            metadata["selector"] = selector
            safe_print(f"Selector resolved for fallback: {selector}")
        else:
            metadata["selector"] = ""
            safe_print(f"Discarding invalid selector returned: {selector}")
    except Exception as e:
        metadata["selector"] = ""
        safe_print(f"Failed to resolve selector for fallback step: {e}")

    # Prompt GPT to write fallback function
    fallback_prompt = f"""You are a Python Playwright automation developer.

The test step was: "{step}"

Here is the parsed metadata:
- Action: {metadata['action']}
- Target element: {metadata['target_element']}
- Field type: {metadata['field_description']}
- Selector (if available): {metadata['selector']}

Write a Python function to perform this action using Playwright.

Guidelines:
- If a selector is provided, use it directly.
- If no selector is available, reason based on the metadata:
  - Think about what kind of element this is (e.g., icon, link, button)
  - Think about what attributes (like  alt, aria-label, text, role but not limited to these only) would help find it
  - Consider common HTML structures, but DO NOT hardcode one pattern
  - Select the most reliable method to locate and click the element
- Your goal is to perform the intended action reliably and accurately

Use the variable `current_page` to access the page.

Return only this format:

```python
def generated_method_name(current_page):
    # implementation
```"""

    response = call_openai(fallback_prompt)

    if hasattr(response, "choices"):
        code = response.choices[0].message.content.strip()
    else:
        code = response.strip()

    if code.startswith("```python"):
        code = code[len("```python"):].strip()
    if code.endswith("```"):
        code = code[:-3].strip()

    safe_print(f"Generated fallback function:\n```python\n{code}\n```")

    try:
        fn_name = re.search(r"def ([a-zA-Z_][a-zA-Z0-9_]*)\(", code).group(1)
        local_vars = {"current_page": current_page}
        exec(code, globals(), local_vars)
        local_vars[fn_name](current_page)
        return "PASS", "fallback", "generated runtime", code
    except Exception as e:
        import traceback
        safe_print(f"Error during fallback execution: {e}")
        traceback.print_exc()
        return "FAIL", "fallback", f"Error: {e}", code


def is_intent_valid(step: str, intent: dict) -> bool:
    step_lower = step.lower()
    action = intent.get("action", "")
    value = intent.get("value", "")

    if not action or not isinstance(value, str):
        return False

    if "click" in step_lower and action == "navigate" and not value.startswith("http"):
        return False

    if action == "navigate" and not ("navigate to" in step_lower or "go to" in step_lower):
        return False

    if action == "type" and not value.strip():
        return False

    return True


def generate_html_report(scenarios, session_id):
    report_dir = os.path.join("reports", session_id)
    os.makedirs(report_dir, exist_ok=True)
    file_path = os.path.join(report_dir, "report.html")

    total = len(scenarios)
    passed = sum(1 for s in scenarios if s["status"] == "PASS")
    failed = total - passed

    with open(file_path, "w", encoding="utf-8") as f:
        f.write("<html><head><title>Test Report</title>")
        f.write("""
        <style>
            body {
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 20px;
            }
            table {
                border-collapse: collapse;
                width: 100%;
                margin-bottom: 20px;
            }
            th, td {
                border: 1px solid #ccc;
                padding: 8px;
                text-align: left;
            }
            th {
                background-color: #f2f2f2;
            }
            summary {
                font-size: 18px;
                font-weight: bold;
                cursor: pointer;
            }
            .pass {
                color: green;
            }
            .fail {
                color: red;
            }
        </style>
        """)
        f.write("</head><body>")
        f.write("<h1>Test Report</h1>")

        # Summary Box
        f.write(f"""
        <div style='
            margin: 20px 0;
            padding: 12px 16px;
            border: 1px solid #ccc;
            background-color: #f9f9f9;
            border-radius: 8px;
            font-size: 14px;
            width: fit-content;
            box-shadow: 0 2px 6px rgba(0,0,0,0.05);
        '>
            <b>Summary:</b><br>
            Total Scenarios: {total}<br>
            Passed: <span style='color:green;'>{passed}</span><br>
            Failed: <span style='color:red;'>{failed}</span>
        </div>
        """)

        for scenario in scenarios:
            scenario_name = scenario["name"]
            overall_status = scenario["status"]
            steps = scenario["steps"]
            color = "green" if overall_status == "PASS" else "red"

            f.write(f"<details open>")
            f.write(f"<summary>{scenario_name} — <span style='color:{color};'><b>{overall_status}</b></span></summary>")
            f.write("<table>")
            f.write("<tr><th>#</th><th>Step</th><th>Action</th><th>Selector</th><th>Status</th></tr>")

            for step in steps:
                step_color = "green" if step["status"] == "PASS" else "red"
                status_label = "Passed" if step["status"] == "PASS" else "Failed"
                f.write("<tr>")
                f.write(f"<td>{step['step_number']}</td>")
                f.write(f"<td>{step['step_text']}</td>")
                f.write(f"<td>{step['action']}</td>")
                f.write(f"<td>{step['selector']}</td>")
                f.write(f"<td style='color:{step_color};'><b>{status_label}</b></td>")
                f.write("</tr>")

            f.write("</table>")
            f.write("</details><br>")

        f.write("</body></html>")

    safe_print(f"HTML report written to: {file_path}")


def wait_for_dependent_dropdown_change(page, before_state, timeout=8000):
    import time
    start = time.time()
    
    while (time.time() - start) * 1000 < timeout:
        after_state = page.evaluate("""() => {
            const all = [];
            for (const sel of document.querySelectorAll('select')) {
                const opts = Array.from(sel.options).map(o => o.textContent.trim());
                all.push({ selector: sel.id || sel.name || sel.outerHTML.slice(0, 100), options: opts });
            }
            return all;
        }""")
        if after_state != before_state:
            # print("Dependent dropdowns updated.")
            return
        page.wait_for_timeout(500)
    # print("No dropdown change detected after selection.")

def detect_new_tab(context, known_pages):
    """
    Checks if a new tab has opened in the current context.
    Returns the new tab if found, else None.
    """
    current_pages = context.pages
    for tab in current_pages:
        if tab not in known_pages:
            return tab
    return None


def select_dropdown(page, selector, label):
    """Handles both native <select> dropdowns and fake ones like AEM/Strayer.
    Also waits for dependent dropdowns to update after selection."""
    
    # Capture dropdown state BEFORE selection
    dropdown_state_before = page.evaluate("""() => {
        const all = [];
        for (const sel of document.querySelectorAll('select')) {
            const opts = Array.from(sel.options).map(o => o.textContent.trim());
            all.push({ selector: sel.id || sel.name || sel.outerHTML.slice(0, 100), options: opts });
        }
        return all;
    }""")

    try:
        # Try native dropdown first
        page.select_option(selector, label=label)
        actual = page.evaluate(f'document.querySelector("{selector}").selectedOptions[0]?.label')
        if label.lower() not in (actual or "").lower():
            raise Exception("Label not found in selected option")
    except Exception:
        # Fallback for fake dropdowns
        page.click(selector)
        page.wait_for_timeout(500)
        option = page.locator(f"text={label}").first
        option.wait_for(timeout=5000)
        option.scroll_into_view_if_needed()
        option.click()
        page.wait_for_timeout(1000)

    # Wait for dependent dropdowns (if any) to change after selection
    wait_for_dependent_dropdown_change(page, dropdown_state_before)

def dismiss_modal_if_any(page):
    print("Checking for modals/popups...")

    modal_selectors = [
        "button:has-text('Accept')",
        "button:has-text('Allow All')",
        "button:has-text('Allow')",
        "button:has-text('Agree')",
        "button:has-text('OK')",
        "button:has-text('Okay')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Accept All')",
        "button:has-text('Close')",
        "button:has-text('×')",
        "button:has-text('Don't Allow')",
        "button:has-text('Reject')",
        "text='×'",
        "#moe-dontallow_button",
        "#optInText",
        "[id*='accept']",
        "[id*='consent']",
        "[class*='accept']",
        "[class*='consent']",
        "[id*='popup'] button",
        "[role='dialog'] button"
    ]

    # Give up to 7 seconds in total, but exit early if a modal is found
    for _ in range(7):
        for selector in modal_selectors:
            try:
                btn = page.locator(selector)
                if btn.count() > 0 and btn.first.is_visible():
                    # safe_print(f"Dismissing modal with selector: {selector}")
                    btn.first.click(timeout=3000)
                    page.wait_for_timeout(1000)
                    return True
            except Exception:
                pass
        page.wait_for_timeout(1000)

    # print("No modal found.")
    return False


def extract_fallback_metadata(step):
    prompt = f"""You are a test step parser.

Break the following test step into structured metadata.

Step: "{step}"

Return ONLY the following JSON:
{{
  "action": "describe what action is expected",
  "target_element": "text or label of the element",
  "field_description": "type of element (button, link, icon, dropdown, etc)",
  "extra": "any additional instruction like press Enter, hover etc"
}}"""

    c = call_openai(prompt)
    c = re.sub(r"^```[a-zA-Z]*\s*", "", c)
    c = re.sub(r"\s*```$", "", c).strip()
    return json.loads(c)


def parse_test_file(file_path):
    scenarios = []
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Split by "Summary:" but preserve the keyword using lookahead
    blocks = re.split(r"(?=Summary:)", content)

    for block in blocks:
        summary_match = re.search(r"Summary:\s*(.+)", block)
        steps_match = re.search(r"Test Steps:\s*(.+)", block, re.DOTALL)

        if summary_match and steps_match:
            scenario_name = summary_match.group(1).strip()
            steps_raw = steps_match.group(1).strip()
            steps = [s.strip() for s in steps_raw.splitlines() if s.strip()]
            scenarios.append({
                "name": scenario_name,
                "steps": steps
            })

    return scenarios


def extract_intent(step):
    if step in intent_cache:
        return intent_cache[step]

    step_lower = step.lower()

    ALLOWED_ACTIONS = {
        "navigate": ["navigate", "go to", "open url"],
        "click": ["click", "press", "tap"],
        "type": ["type", "fill", "enter", "input"],
        "select": ["select"],
        "checkbox": ["checkbox", "tick", "mark checkbox"],
        "check_url": ["check url", "verify url", "validate url", "confirm url"],
        "check_title": ["check title", "verify title", "validate title", "confirm title"],
        "check_text": ["check text", "verify text", "validate text", "confirm text", "ensure text"],
        "radio": ["radio"],
        "performance_check": ["performance", "lighthouse", "speed test"],
        "accessibility_check": ["accessibility", "axe", "a11y"],
        "hover": ["hover", "mouse over", "move over"]
    }

    # Try to pre-identify the described action in the step
    likely_action = None
    # Match loosely against all synonyms
    for action, keywords in ALLOWED_ACTIONS.items():
        for keyword in keywords:
            if keyword in step_lower:
                likely_action = action
                break
        if likely_action:
            break

    # If no valid action is found in the step → go to fallback
    if not likely_action:
        safe_print(f"No valid action found in step → using fallback: {step}")
        return { "action": "custom_fallback", "step": step }

    # If a valid action is described, now ask GPT to build full intent
    prompt = f"""You are a test automation planner.

Convert the following step into a structured JSON intent so the automation engine can understand and act on it.

Supported actions:
- {', '.join(ALLOWED_ACTIONS)}

Step: "{step}"

Return ONLY the following JSON. Do not explain anything or wrap it in markdown:
{{
  "action": "navigate|click|type|select|checkbox|check_url|check_title|check_text|radio|performance_check|accessibility_check|hover",
  "value": "...",
  "field_description": "...",
  "press": "Enter"
}}"""

    c = call_openai(prompt)
    c = re.sub(r"^```[a-zA-Z]*\s*", "", c)
    c = re.sub(r"\s*```$", "", c).strip()

    try:
        intent = json.loads(c)
    except json.JSONDecodeError:
        safe_print(f"Failed to parse GPT response as JSON → fallback for step: {step}")
        return { "action": "custom_fallback", "step": step }

    intent.setdefault("press", "")
    action = intent.get("action", "")

    # Final sanity check — if GPT hallucinated an unsupported action, fallback
    if action not in ALLOWED_ACTIONS:
        safe_print(f"GPT returned unsupported action → fallback for step: {step}")
        return { "action": "custom_fallback", "step": step }

    intent_cache[step] = intent
    return intent


def extract_candidates(page):
    return page.evaluate("""
        () => {
            const elements = [];
            const all = document.querySelectorAll('input, button, a, select, textarea, label, div[role=checkbox]');

            function isVisible(el) {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return (
                    el.offsetParent !== null &&
                    style.visibility !== 'hidden' &&
                    style.display !== 'none' &&
                    rect.width > 0 &&
                    rect.height > 0
                );
            }

            function getSectionPath(el) {
                const path = [];
                let current = el.parentElement;

                while (current) {
                    const tag = current.tagName.toLowerCase();
                    const isLabel = ["h1", "h2", "h3", "h4", "h5", "h6", "legend", "strong", "span", "label"].includes(tag);
                    const text = current.innerText?.trim();

                    if (isLabel && text && text.length < 100) {
                        path.unshift(text);
                    }
                    current = current.parentElement;
                }
                return path.join(" > ");
            }

            for (const el of all) {
                if (!isVisible(el)) continue;

                const tag = el.tagName.toLowerCase();
                const type = el.type || '';
                const role = el.getAttribute('role') || '';
                const ariaLabel = el.getAttribute('aria-label') || '';
                const labelEl = el.labels && el.labels.length > 0 ? el.labels[0] : null;
                const label = labelEl?.innerText || el.innerText || ariaLabel || el.placeholder || '';

                let selector = tag;
                if (el.id) selector += `#${el.id}`;
                else if (el.name) selector += `[name='${el.name}']`;

                const isClickable = (
                    typeof el.onclick === 'function' ||
                    tag === 'button' ||
                    tag === 'a' ||
                    el.getAttribute('role') === 'button' ||
                    el.getAttribute('tabindex') !== null ||
                    window.getComputedStyle(el).cursor === 'pointer'
                );

                elements.push({
                    tag: tag,
                    text: label.trim(),
                    selector: selector,
                    section_path: getSectionPath(el),
                    clickable: isClickable,
                    attributes: {
                        type: type,
                        role: role,
                        name: el.name || '',
                        id: el.id || '',
                        value: el.value || '',
                        ariaLabel: ariaLabel,
                        placeholder: el.placeholder || '',
                        tabindex: el.getAttribute('tabindex') || '',
                        class: el.className || ''
                    }
                });
            }

            return elements.slice(0, 100);
        }
    """)

def match_selector(step, intent, candidates, page):
    # Step 0: Short-circuit if explicit locator is provided
    if 'explicit_locator' in intent and intent['explicit_locator']:
        sel = intent['explicit_locator'].strip()
        #safe_print(f"[USING EXPLICIT LOCATOR] Selector provided in step: {sel}")
        return sel
    
    cache_key = step.strip().lower()
    if cache_key in selector_cache:
        # print("Using cached selector for this step")
        return selector_cache[cache_key]

    def compute_score(candidate, intent):
        score = 0
        intent_text = f"{intent.get('target', '')} {intent.get('value', '')}".strip().lower()
        candidate_text = candidate.get("text", "").lower()
        section_path = candidate.get("section_path", "").lower()

        if intent_text in candidate_text:
            score += 10
        if intent_text in section_path:
            score += 15

            # Match against <input value="..."> text for buttons/submit inputs
        input_value = candidate.get("attributes", {}).get("value", "").lower()
        if intent_text in input_value:
            score += 10

        sim = difflib.SequenceMatcher(None, intent_text, candidate_text).ratio()
        score += int(sim * 10)

        # Treat input[type=submit] and input[type=button] like buttons
        tag = candidate.get("tag", "")
        input_type = candidate.get("attributes", {}).get("type", "").lower()

        is_button_like = (
            tag in ["button", "a"]
            or (tag == "input" and input_type in ["submit", "button"])
            or candidate.get("clickable")
        )

        if is_button_like:
            score += 2

        return score
    
    # Remove nav-assist, tooltips, shortcuts
    def is_junk(c):
        sel = c.get("selector", "").lower()
        txt = c.get("text", "").lower()
        return any(bad in sel or bad in txt for bad in ["nav-assist", "tooltip", "shortcut"])

    candidates = [c for c in candidates if not is_junk(c)]

    scored = [(compute_score(c, intent), c) for c in candidates]
    scored.sort(reverse=True, key=lambda x: x[0])

    # Use if high-confidence
    best = scored[0][1]
    if scored[0][0] >= 25 and best["selector"].strip().lower() not in ["a", "div", "span", "label", "input", "button"]:
        best = scored[0][1]
        safe_print(f"[HEURISTIC] Best match: {best['text']} @ {best['selector']} (Score: {scored[0][0]})")
        return best["selector"]

    prompt = f"""You are an AI test step matcher.

You are given:
- A test step
- A structured intent (action, value, field description)
- A list of elements on the page

Each element has the following info:
- tag
- text (label, aria, placeholder)
- selector
- whether it is visibly clickable (boolean)

Choose the most appropriate element that matches the user's intent.
- Prefer elements that are `clickable: true` when action is 'click'
- Match by intent text/field/label
- If the intent refers to the "first" or "top" element, prefer elements that appear earliest in the list (i.e. lower index).
- Important: Avoid elements that appear to be tooltips, assistive nav items, or keyboard shortcuts (e.g. 'nav-assist', 'shortcut', etc.)
- Ignore hidden or non-clickable elements for click actions
- Return only the best matching element's `selector`.

Step: "{step}"
Intent: {json.dumps(intent)}
Candidates: {json.dumps(candidates, indent=2)}

Only return the selector string (e.g., `button#submit`). No formatting or explanation.
"""
    
    print("[DEBUG] match_selector candidates:")
    for idx, cand in enumerate(candidates):
        text = f"{idx+1}: {cand['selector']} | text='{cand['text']}' clickable={cand.get('clickable')}"
        # print(text.encode('ascii', 'backslashreplace').decode())
    sel = call_openai(prompt)
    # sel = re.sub(r"^```[a-zA-Z]*\s*", "", sel)
    # sel = re.sub(r"\s*```$", "", sel).strip()
    sel = sel.strip()
    if sel.startswith("```") and sel.endswith("```"):
        sel = "\n".join(sel.strip("`").splitlines()[1:]).strip()
    sel = sel.strip("`")

# Prevent generic selectors from being used
    if sel.strip().lower() in ["a", "div", "span", "label", "input", "button", "none", "null", ""]:
        print("No valid selector from OpenAI. Using fallback locator crawler...")

        fallback_candidates = extract_locators_from_page(page)
        
        # Filter out overly generic fallback elements
        filtered_fallback = [
            c for c in fallback_candidates
            if c["selector"] not in ["a", "div", "span", "label", "input", "button", None]
            and c["text"].strip() != ""
            and not is_junk(c)
        ]

        print("[DEBUG] filtered fallback candidates:")
        for i, cand in enumerate(filtered_fallback):
            text = f"{i+1}: {cand['selector']} | text='{cand['text']}' clickable={cand.get('clickable')}"
            # print(text.encode('ascii', 'backslashreplace').decode())

        prompt_fallback = f"""You are an AI fallback matcher.

Pick the most appropriate element from the fallback candidate list that best matches the intent.

Step: "{step}"
Intent: {json.dumps(intent)}
Fallback Candidates: {json.dumps(filtered_fallback, indent=2)}

Return ONLY the selector (as plain text) with no formatting or explanation:
"""
        sel = call_openai(prompt_fallback)
        # sel = re.sub(r"^```[a-zA-Z]*\s*", "", sel)
        # sel = re.sub(r"\s*```$", "", sel).strip()
        sel = sel.strip()
        if sel.startswith("```") and sel.endswith("```"):
            sel = "\n".join(sel.strip("`").splitlines()[1:]).strip()
        sel = sel.strip("`")

    safe_print(f"Final selector to return: {sel!r}")
    selector_cache[cache_key] = sel
    return sel


def scroll_to(page, selector, max_attempts=10):
    locator = page.locator(selector).first  # ensure we pick the first matching one
    for attempt in range(max_attempts):
        try:
            if locator.is_visible():
                safe_print(f"Element is now visible: {selector}")
                return

            safe_print(f"Attempting scroll #{attempt+1} for {selector}")
            page.evaluate("window.scrollBy(0, 300)")
            page.wait_for_timeout(500)  # Allow time for lazy load or animations
        except Exception as e:
            safe_print(f"Scroll attempt failed: {e}")
            page.wait_for_timeout(500)

    # Final fallback: try Playwright's explicit wait
    try:
        page.wait_for_selector(selector, timeout=3000)
        if page.locator(selector).first.is_visible():
            safe_print(f"Element became visible after explicit wait: {selector}")
            return
    except:
        pass

    raise Exception(f"Element not visible even after scrolling: {selector}")


def run_test(session_id, headless=False):
    # Clean any previous data for this session
    shutil.rmtree(os.path.join("reports", session_id), ignore_errors=True)
    shutil.rmtree(os.path.join("screenshots", session_id), ignore_errors=True)

    Path(os.path.join("reports", session_id)).mkdir(parents=True, exist_ok=True)
    Path(os.path.join("screenshots", session_id)).mkdir(parents=True, exist_ok=True)
    os.makedirs("reports", exist_ok=True)
    all_scenarios = parse_test_file(TEST_FILE)
    all_report_data = []
    report_rows = []
    overall_status = "PASS"
    scenario_code_map = {}
    

    with sync_playwright() as p:
        # browser = p.chromium.launch(headless=headless)
        if BROWSER_TYPE == 'chromium':
            browser = p.chromium.launch(headless=headless)
        elif BROWSER_TYPE == 'firefox':
            browser = p.firefox.launch(headless=headless)
        elif BROWSER_TYPE == 'webkit':
            browser = p.webkit.launch(headless=headless)
        else:
            raise ValueError(f"Unsupported browser type: {BROWSER_TYPE}")
        context = browser.new_context(
            permissions=[],  # Deny notification modals
            ignore_https_errors=True
        )
        page = browser.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        previous_dom_hash = None
        cached_candidates = []
        original_page = page
        current_page = page
        def sanitize_folder_name(name):
            return re.sub(r'[^a-zA-Z0-9_\-]', '_', name)

        for scenario in all_scenarios:
            scenario_name = scenario["name"]
            steps = scenario["steps"]
                    # Place this ONCE before starting the scenario loop (not per step):
            scenario_name_clean = sanitize_folder_name(scenario["name"])
            scenario_folder = os.path.join("screenshots", scenario_name_clean)

                    # Clear folder if it exists, then create fresh
            if os.path.exists(scenario_folder):
                shutil.rmtree(scenario_folder)
            os.makedirs(scenario_folder, exist_ok=True)

            safe_print(f"\nRunning Scenario: {scenario_name}")
            report_rows = []
            overall_status = "PASS"
            current_page = page
            original_page = page

            for idx, step in enumerate(steps):
                # current_page.wait_for_timeout(1000)  # Wait 1 sec before the step starts
                step_lower = step.lower()
                step_code = None
                current_row = {
                    "step_number": idx + 1,
                    "step_text": step,
                    "action": "",
                    "selector": "",
                    "status": "PASS"
                }

                manual_selector = extract_locator_if_present(step)

                if "in new tab" in step_lower or "in the new tab" in step_lower:
                    all_pages = original_page.context.pages
                    new_tab = next((p for p in all_pages if p != original_page), None)
                    if new_tab:
                        current_page = new_tab
                        # print("Switched to new tab")
                        current_page.wait_for_load_state("domcontentloaded", timeout=10000)
                        dismiss_modal_if_any(current_page) #----Uncomment later
                        current_page.wait_for_timeout(1000)
                    else:
                        raise Exception("No new tab found")
                    # report.append(" Step passed")
                    report_rows.append(current_row)
                    # continue  # No other action needed for this step

                elif "close new tab" in step_lower or "close the new tab" in step_lower:
                    if current_page != original_page:
                        current_page.close()
                        current_page = original_page
                        # print("Closed new tab and switched back to original")
                    # else:
                    #     print("Current tab is already the original; no tab closed.")
                    # report.append(" Step passed")
                    report_rows.append(current_row)
                    continue

                elif "switch to window with title" in step_lower:
                    try:
                        # Extract window title from step
                        expected_title = step_lower.split("switch to window with title")[-1].strip().strip('"').strip("'")

                        def switch_window_by_title(context, expected_title, timeout=5000):
                            start = time.time()
                            while (time.time() - start) * 1000 < timeout:
                                for page in context.pages:
                                    try:
                                        title = page.title().strip().lower()
                                        if expected_title.lower() in title:
                                            return page
                                    except:
                                        continue
                                time.sleep(0.5)
                            raise Exception(f"No window with title containing '{expected_title}' found.")

                        new_window = switch_window_by_title(original_page.context, expected_title)

                        if new_window:
                            current_page = new_window
                            current_page.wait_for_load_state("domcontentloaded", timeout=10000)
                            dismiss_modal_if_any(current_page)  # Uncomment later
                            current_page.wait_for_timeout(1000)
                        else:
                            raise Exception(f"Window with title '{expected_title}' not found.")

                        report_rows.append(current_row)

                    except Exception as e:
                        raise Exception(f"Failed to switch window by title: {str(e)}")
                    continue

                elif "switch to original tab" in step_lower or "switch to main tab" in step_lower or "in original tab" in step_lower or "in main tab" in step_lower or "switch to the original tab" in step_lower or "switch to the main tab" in step_lower or "in the original tab" in step_lower or "in the main tab" in step_lower:
                    current_page = original_page
                    # print("Switched back to original tab")
                    # report.append(" Step passed")
                    report_rows.append(current_row)
                    # continue

                intent = extract_intent(step)
                intent["explicit_locator"] = extract_locator_if_present(step) 
                safe_print(f"[DEBUG] Step: {step}")
                safe_print(f"[DEBUG] Intent: {intent}")
                current_row["action"] = intent.get("action", "")
                if manual_selector:
                    intent["selector"] = manual_selector
                    intent["selector_source"] = "manual"

                try:
                    if intent["action"] == "custom_fallback":
                        cached_candidates = extract_locators_from_page(current_page)
                        try:
                            status, action, selector, code = handle_unknown_step(step, current_page, cached_candidates)
                            step_code = code
                        except Exception as e:
                            safe_print(f"Fallback crashed entirely: {e}")
                            # traceback.print_exc()
                            status, action, selector, code = "FAIL", "fallback", "", ""
                            step_code = code

                        current_row = {
                            "step_number": idx + 1,
                            "step_text": step,
                            "action": action,
                            "selector": selector,
                            "status": status
                        }

                        if status == "FAIL":
                            overall_status = "FAIL"

                        report_rows.append(current_row)
                        scenario_code_map.setdefault(scenario_name, []).append(step_code or f"# No code for step: {step}")
                        continue
                    elif intent["action"] == "navigate":
                        step_code = (
                            f'def step_{idx+1}_navigate(current_page):\n'
                            f'    current_page.goto("{intent["value"]}", timeout=60000)\n'
                            f'    current_page.wait_for_load_state("domcontentloaded", timeout=5000)\n'
                            f'    current_page.wait_for_timeout(2000)\n'
                            f'    # dismiss_modal_if_any(current_page)\n'
                        )
                        try:
                            current_page.goto(intent["value"], timeout=60000)
                            current_page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except TimeoutError:
                            print("Warning: Page load took too long, continuing anyway...")

                        current_page.wait_for_timeout(2000)
                        dismiss_modal_if_any(current_page) #---uncomment later
                        scenario_code_map.setdefault(scenario_name, []).append(step_code or f"# No code for step: {step}")
                    
                    elif intent["action"] == "performance_check":
                        url = current_page.url
                        report_path = os.path.join("reports", session_id)
                        os.makedirs(report_path, exist_ok=True)
                        # safe_print(f"Running Lighthouse on {url}")
                        #For mobile
                        os.system(f"lighthouse {url} --quiet --output html --output-path={report_path}/lighthouse_report.html --chrome-flags='--headless'")
                        #For desktop
                        os.system(f"lighthouse {url} --quiet --output html --output-path={report_path}/lighthouse_report.html --chrome-flags='--headless' --preset=desktop")
                        # report.append("Performance report generated (Lighthouse)")
                        # report_rows.append(current_row)

                    elif intent["action"] == "accessibility_check":
                        axe_script = open("axe.min.js", encoding="utf-8").read()
                        current_page.evaluate(axe_script)
                        results = current_page.evaluate("async () => await axe.run(document)")

                        # Save pretty HTML report only
                        html = "<html><head><title>Axe Accessibility Report</title></head><body>"
                        html += "<h1>Accessibility Violations</h1>"

                        if results["violations"]:
                            for violation in results["violations"]:
                                html += f"<h2 style='color:red'>{violation['help']}</h2>"
                                html += f"<p>{violation['description']}</p><ul>"
                                for node in violation["nodes"]:
                                    html += f"<li><code>{node['html']}</code> - {', '.join(node['target'])}</li>"
                                html += "</ul>"
                        else:
                            html += "<p style='color:green'>No accessibility violations found!</p>"

                        html += "</body></html>"
                        report_path = os.path.join("reports", session_id)
                        os.makedirs(report_path, exist_ok=True)

                        with open(os.path.join(report_path, "axe_report.html"), "w", encoding="utf-8") as f:
                            f.write(html)

                    elif intent["action"] in ["type", "click", "select", "checkbox", "hover"]:
                        # Compute DOM hash
                        html = current_page.content()
                        dom_hash = hashlib.sha256(html.encode()).hexdigest()

                        if dom_hash != previous_dom_hash:
                            # print("[DEBUG] DOM changed — re-extracting candidates")
                            cached_candidates = extract_candidates(current_page)
                            previous_dom_hash = dom_hash
                        # else:
                        #     print("[DEBUG] DOM unchanged — using cached candidates")

                        candidates = cached_candidates
                        # New: Check if the step has a manual locator provided
                        sel = match_selector(step, intent, candidates, current_page)  # ✅ Unified logic
                        safe_print(f"Final selector returned by match_selector: {sel!r}")
                        current_row["selector"] = sel
                        scroll_to(current_page, sel)

                        if intent["action"] == "type":
                            current_page.fill(sel, intent["value"])
                            current_page.wait_for_timeout(1000)
                            if intent.get("press"):
                                locator = current_page.locator(sel).first
                                locator.press(intent["press"])  # e.g., "Enter"
                            locator = current_page.locator(sel).first
                            actual = locator.input_value()
                            if actual.strip() != intent["value"]:
                                raise Exception(f"Value mismatch: Expected '{intent['value']}' but found '{actual}'")
                            
                        elif intent["action"] == "hover":
                            step_code = (
                                f'def step_{idx+1}_hover(current_page):\n'
                                f'    locator = current_page.locator("{sel}").first\n'
                                f'    if not locator.is_visible():\n'
                                f'        raise Exception("Element \'{sel}\' not visible before hover")\n'
                                f'    box = locator.bounding_box()\n'
                                f'    if not box:\n'
                                f'        raise Exception("Could not determine bounding box for {sel}")\n'
                                f'    center_x = box["x"] + box["width"] / 2\n'
                                f'    center_y = box["y"] + box["height"] / 2\n'
                                f'    current_page.mouse.move(center_x, center_y)\n'
                                f'    current_page.evaluate("""() => {{\n'
                                f'        const el = document.evaluate("//*[contains(text(), \'{intent["field_description"]}\')]", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;\n'
                                f'        if (el) {{\n'
                                f'            el.dispatchEvent(new MouseEvent(\'mouseover\', {{ bubbles: true }}));\n'
                                f'        }}\n'
                                f'    }}""")\n'
                                f'    current_page.wait_for_timeout(800)\n'
                            )
                            locator = current_page.locator(sel).first

                            if not locator.is_visible():
                                raise Exception(f"Element '{sel}' not visible before hover")

                            # safe_print(f" Hovering on: {sel}")

                            # Step 1: Physically move mouse to the center of the element
                            box = locator.bounding_box()
                            if not box:
                                raise Exception(f"Could not determine bounding box for {sel}")

                            center_x = box["x"] + box["width"] / 2
                            center_y = box["y"] + box["height"] / 2
                            current_page.mouse.move(center_x, center_y)
                            
                            # Step 2: Use JS to keep it focused (prevents submenu vanishing)
                            current_page.evaluate(
                                f"""() => {{
                                    const el = document.evaluate("//*[contains(text(), '{intent['field_description']}')]", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                                    if (el) {{
                                        el.dispatchEvent(new MouseEvent('mouseover', {{ bubbles: true }}));
                                    }}
                                }}"""
                            )

                            # Step 3: Short wait to allow menu to appear
                            current_page.wait_for_timeout(800)
                            scenario_code_map.setdefault(scenario_name, []).append(step_code or f"# No code for step: {step}")

                            # safe_print(f"Mouse over completed and held: {sel}")


                        elif intent["action"] == "click":
                            step_code = (
                                f'def step_{idx+1}_click(current_page):\n'
                                f'    locator = current_page.locator("{sel}").first\n'
                                f'    locator.click()\n'
                            )
                            current_page.wait_for_timeout(1000) 
                            scroll_to(current_page, sel)
                            # page.click(sel)
                            locator = current_page.locator(sel).first
                            if not locator.is_visible():
                                raise Exception(f"Element '{sel}' not visible before click")
                            known_pages = current_page.context.pages.copy()
                            locator.click()
                            time.sleep(1)
                            new_tab = detect_new_tab(current_page.context, known_pages)
                            scenario_code_map.setdefault(scenario_name, []).append(step_code or f"# No code for step: {step}")
                            # if new_tab:
                            #     # print("Detected new tab and saved for future use") 

                        elif intent["action"] == "select":
                            select_dropdown(current_page, sel, intent["value"])

                        elif intent["action"] == "checkbox":
                            current_page.check(sel)

                        elif intent["action"] == "radio":
                            current_page.check(sel)


                    elif intent["action"] == "check_url":
                        if intent["value"] not in current_page.url:
                            raise Exception(f"Expected {intent['value']} in {current_page.url}")

                    elif intent["action"] == "check_title":
                        actual = current_page.title().strip()
                        expected = intent["value"].strip()
                        if expected.lower() not in actual.lower():
                            raise Exception(f"Title mismatch: expected '{expected}' but got '{actual}'")
                        
                    elif intent["action"] == "switch_to_tab_with_title":
                        target_title = intent["target"]
                        context = current_page.context
                        found = False
                        for p in context.pages:
                            try:
                                time.sleep(1)
                                title = p.title()
                                if target_title.lower() in title.lower():
                                    current_page = p  # Switch focus
                                    # safe_print(f"Switched to tab with title: {title}")
                                    found = True
                                    break
                            except:
                                safe_print(f"Failed to read title: {e}")
                                continue
                        if not found:
                            safe_print(f"Could not find tab with title containing '{target_title}'")

                    elif intent["action"] == "check_text":
                        page_text = current_page.inner_text("body")
                        expected = intent["value"].strip()
                        
                        if expected.lower() not in page_text.lower():
                            raise Exception(f"Text '{expected}' not found on page")

                        # Only check bold if mentioned in the original test step
                        if "bold" in step.lower():
                            styles = current_page.evaluate(f'''
                                () => {{
                                    const el = document.evaluate(
                                        "//*[contains(text(), '{intent['value']}')]",
                                        document,
                                        null,
                                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                                        null
                                    ).singleNodeValue;
                                    if (!el) return null;
                                    const style = window.getComputedStyle(el);
                                    return {{
                                        fontWeight: style.fontWeight,
                                        tagName: el.tagName
                                    }};
                                }}
                            ''')

                            if not styles:
                                raise Exception(f"'{intent['value']}' not found in page")

                            font_weight = styles["fontWeight"]
                            tag = styles["tagName"]

                            if tag in ["B", "STRONG"] or font_weight in ["bold", "bolder"] or int(font_weight) >= 600:
                                pass  # It's bold
                            else:
                                raise Exception(f"Text '{intent['value']}' found but not bold")


                except Exception as e:
                    safe_print(f"ERR@_S{idx+1}: {e}")
                    import traceback
                    # traceback.print_exc()

                    safe_print(f"Sending to fallback: {step}")
                    try:
                        status, action, selector = handle_unknown_step(step, current_page, cached_candidates)
                    except Exception as fallback_err:
                        safe_print(f"Fallback failed: {fallback_err}")
                        # traceback.print_exc()
                        status, action, selector = "FAIL", "fallback", ""

                    current_row["status"] = status
                    current_row["action"] = action
                    current_row["selector"] = selector

                    if status == "FAIL":
                        overall_status = "FAIL"

                report_rows.append(current_row)

                # scenario_folder = os.path.join("screenshots", session_id, scenario_name_clean)
                # # os.makedirs(screenshots_dir, exist_ok=True)
                # try:
                #     current_page.wait_for_timeout(1000)
                #     screenshot_path = os.path.join(scenario_folder, f"step_{idx+1}.png")
                #     current_page.screenshot(path=screenshot_path, timeout=10000)
                # except Exception as ss_err:
                #     safe_print(f"Screenshot failed at step {idx+1}: {ss_err}")
        
        # Collect results
            all_report_data.append({
                "name": scenario_name,
                "status": overall_status,
                "steps": report_rows
            })

        browser.close()
        # Save all generated code per scenario
        code_dir = os.path.join("generated_code")
        os.makedirs(code_dir, exist_ok=True)
        for scenario, code_list in scenario_code_map.items():
            safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', scenario)
            code_path = os.path.join(code_dir, f"{safe_name}.py")
            with open(code_path, "w", encoding="utf-8") as f:
                f.write("# Generated code for scenario: " + scenario + "\n\n")
                for code in code_list:
                    f.write((code or "") + "\n\n")
            print(f"Saved generated code for scenario '{scenario}' to {code_path}")


    # print("\nOpenAI Usage Summary:")
    # safe_print(f"→ Prompt tokens used: {total_prompt_tokens}")
    # safe_print(f"→ Completion tokens used: {total_completion_tokens}")
    # safe_print(f"→ Total calls made: {total_calls}")
    # safe_print(f"Estimated total cost: ${total_cost_usd:.5f}")

    generate_html_report(all_report_data, session_id) 

def generate_runtime_zip_report(session_id):
    from io import BytesIO

    zip_buffer = BytesIO()

    # Create folder paths
    temp_base = os.path.join("sessions", session_id)
    report_source = os.path.join("reports", session_id, "report.html")
    lighthouse_source = os.path.join("reports", session_id, "lighthouse_report.html")
    axe_source = os.path.join("reports", session_id, "axe_report.html")
    screenshots_source = os.path.join("screenshots", session_id)

    temp_report_dir = os.path.join(temp_base, "report")
    os.makedirs(temp_report_dir, exist_ok=True)

    # Move HTML report
    if os.path.exists(report_source):
        shutil.copy(report_source, os.path.join(temp_report_dir, "report.html"))

    # Move Lighthouse and Axe reports
    if os.path.exists(lighthouse_source):
        shutil.copy(lighthouse_source, os.path.join(temp_report_dir, "lighthouse_report.html"))

    if os.path.exists(axe_source):
        shutil.copy(axe_source, os.path.join(temp_report_dir, "axe_report.html"))

    # Move screenshots if available
    if os.path.exists(screenshots_source):
        shutil.copytree(screenshots_source, os.path.join(temp_report_dir, "screenshots"), dirs_exist_ok=True)

    # Create ZIP
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(temp_report_dir):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, temp_report_dir)
                zipf.write(full_path, arcname)

    # Save ZIP file to disk (e.g., for Streamlit to serve it)
    os.makedirs("sessions", exist_ok=True)  # ✅ Ensure 'sessions' folder exists
    zip_output_path = os.path.join("sessions", f"{session_id}.zip")
    with open(zip_output_path, "wb") as f:
        f.write(zip_buffer.getvalue())

    # Cleanup temp folder (keep only the zip)
    shutil.rmtree(temp_base, ignore_errors=True)

    # ✅ Clean report + screenshot folders permanently
    shutil.rmtree(os.path.join("reports", session_id), ignore_errors=True)
    shutil.rmtree(os.path.join("screenshots", session_id), ignore_errors=True)

    return zip_output_path

if __name__ == "__main__":
    session_id = os.getenv("SESSION_ID") or str(uuid.uuid4())[:8]  # or receive from user/session
    for f in glob.glob("sessions/*.zip"):
        os.remove(f)
    run_test(session_id)
    generate_runtime_zip_report(session_id)