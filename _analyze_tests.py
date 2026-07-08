import subprocess
import sys
import re
from collections import Counter

result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line",
     "--ignore=tests/test_database_indexes.py",
     "--ignore=tests/test_e2e_functional.py"],
    capture_output=True, text=True, cwd=r"f:\naxida\xiaoda-agent"
, check=False)

errors = Counter()
failed_tests = []
for line in result.stdout.splitlines() + result.stderr.splitlines():
    if line.startswith("FAILED"):
        failed_tests.append(line)
    # Classify errors from tb=line output
    if "ImportError" in line or "ModuleNotFoundError" in line:
        errors["ImportError"] += 1
    elif "AttributeError" in line:
        errors["AttributeError"] += 1
    elif "TypeError" in line:
        errors["TypeError"] += 1
    elif "AssertionError" in line or "AssertionErr" in line:
        errors["AssertionError"] += 1
    elif "NameError" in line:
        errors["NameError"] += 1
    elif "KeyError" in line:
        errors["KeyError"] += 1
    elif "FileNotFoundError" in line:
        errors["FileNotFoundError"] += 1
    elif "SyntaxError" in line:
        errors["SyntaxError"] += 1
    elif "fixture" in line.lower() and "not found" in line.lower():
        errors["FixtureNotFound"] += 1
    elif "collect" in line.lower() and "error" in line.lower():
        errors["CollectionError"] += 1

print(f"Total FAILED: {len(failed_tests)}")
print("\nError type distribution:")
for err, cnt in errors.most_common():
    print(f"  {err}: {cnt}")

# Group by test file
file_counts = Counter()
for t in failed_tests:
    m = re.search(r"FAILED (tests/[^/]+/)", t)
    if m:
        file_counts[m.group(1)] += 1
    else:
        m2 = re.search(r"FAILED (tests/[^:]+)", t)
        if m2:
            file_counts[m2.group(1)] += 1

print("\nFailed by test file:")
for f, c in file_counts.most_common():
    print(f"  {f}: {c}")

# Print first 30 failed test names for analysis
print("\nFirst 30 FAILED tests:")
for t in failed_tests[:30]:
    print(f"  {t}")
