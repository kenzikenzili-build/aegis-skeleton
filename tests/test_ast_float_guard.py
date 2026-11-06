"""Tests for the AEGIS-FLOAT rule of the AST Smite Guard.

The rule lives in ``.kiro/hooks/pre-commit`` (``_check_float_currency_math``)
and enforces Rule 4 of ``.kiro/steering/architecture.md``: arithmetic on a
monetary field (``amount``, ``balance``, ``cost``, ``fee``, ``price``) must use
``decimal.Decimal``, never a float literal or ``float()``.

Standard library only. Run from the repository root with either:

    python3 -m unittest discover -s tests -v
    python3 -m pytest tests/test_ast_float_guard.py
"""

import ast
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_PATH = os.path.join(REPO_ROOT, ".kiro", "hooks", "pre-commit")

MONEY_NAMES = ("amount", "balance", "cost", "fee", "price")
OPERATORS = ("+", "-", "*", "/")


def _load_hook():
    """Import the extension-less hook script as a module (no bytecode written)."""
    with open(HOOK_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    module = types.ModuleType("aegis_smite_hook")
    module.__file__ = HOOK_PATH
    exec(compile(source, HOOK_PATH, "exec"), module.__dict__)
    return module


HOOK = _load_hook()


def float_violations(source):
    """Return only the AEGIS-FLOAT violations the guard reports for ``source``."""
    found, parsed = HOOK._scan_python("sample.py", textwrap.dedent(source))
    assert parsed, "sample source must be valid Python"
    return [item for item in found if item["rule"] == "AEGIS-FLOAT"]


def run_hook(source):
    """Run the hook as a subprocess on ``source`` and return the completed process."""
    with tempfile.TemporaryDirectory() as workdir:
        target = os.path.join(workdir, "sample.py")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(source))
        return subprocess.run(
            [sys.executable, HOOK_PATH, target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            check=False,
        )


class FloatGuardExitCodeTests(unittest.TestCase):
    """The hook must block the commit (exit 1) when it sees float money math."""

    def test_float_literal_multiplication_exits_1(self):
        result = run_hook("fee = amount * 0.029\n")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("AEGIS-FLOAT", result.stdout)
        self.assertIn("amount", result.stdout)

    def test_float_call_exits_1(self):
        result = run_hook("total = float(price) + 1\n")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("AEGIS-FLOAT", result.stdout)

    def test_fee_formula_from_steering_doc_in_float_form_exits_1(self):
        result = run_hook(
            """
            def stripe_fee(gross):
                fee = gross * 0.029 + 0.30
                return fee
            """
        )
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_decimal_arithmetic_exits_0(self):
        result = run_hook(
            """
            from decimal import Decimal, ROUND_HALF_UP

            CENT = Decimal("0.01")

            def stripe_fee(gross):
                fee = gross * Decimal("0.029") + Decimal("0.30")
                return fee.quantize(CENT, rounding=ROUND_HALF_UP)
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("AEGIS-FLOAT", result.stdout)

    def test_hint_points_to_decimal(self):
        result = run_hook("x = cost / 100.0\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("decimal.Decimal", result.stdout)


class FloatGuardDetectionTests(unittest.TestCase):
    """Which expressions are flagged."""

    def test_every_operator_on_every_monetary_name_is_flagged(self):
        for name in MONEY_NAMES:
            for operator in OPERATORS:
                with self.subTest(name=name, operator=operator):
                    found = float_violations("x = {0} {1} 0.5\n".format(name, operator))
                    self.assertEqual(len(found), 1)

    def test_float_literal_on_the_left_is_flagged(self):
        self.assertEqual(len(float_violations("x = 0.5 * price\n")), 1)

    def test_float_call_operand_is_flagged_for_every_operator(self):
        for operator in OPERATORS:
            with self.subTest(operator=operator):
                found = float_violations(
                    "x = balance {0} float(delta)\n".format(operator)
                )
                self.assertEqual(len(found), 1)

    def test_float_call_around_the_monetary_name_is_flagged(self):
        self.assertEqual(len(float_violations("x = float(amount) * 2\n")), 1)

    def test_aliased_builtins_float_is_flagged(self):
        found = float_violations(
            """
            import builtins as b
            x = b.float(price) + 1
            """
        )
        self.assertEqual(len(found), 1)

    def test_monetary_reference_shapes_are_flagged(self):
        shapes = {
            "compound snake_case": "x = net_amount * 2.0",
            "camelCase": "x = totalFee * 1.1",
            "plural": "x = fees * 1.5",
            "attribute": "x = tx.amount * 0.5",
            "subscript key": 'x = row["amount"] * 0.5',
            "dict get": 'x = body.get("price") + 0.01',
            "unary minus": "x = -amount * 0.5",
            "inside a call": "x = round(amount * 0.029, 2)",
        }
        for label, line in shapes.items():
            with self.subTest(shape=label):
                self.assertEqual(len(float_violations(line + "\n")), 1)

    def test_float_nested_in_arithmetic_is_flagged(self):
        self.assertEqual(len(float_violations("x = amount * (1 + 0.029)\n")), 1)

    def test_augmented_assignment_is_flagged(self):
        for line in ("fee *= 0.029", "self.balance += 0.5", "price /= 1.07"):
            with self.subTest(statement=line):
                self.assertEqual(len(float_violations(line + "\n")), 1)

    def test_float_arithmetic_stored_into_a_monetary_name_is_flagged(self):
        # The monetary name is the assignment target, not an operand.
        for line in (
            "fee = gross * 0.029 + 0.30",
            "price: float = base / 1.07",
            "self.balance = opening - 0.5",
            'row["amount"] = units * 2.5',
        ):
            with self.subTest(statement=line):
                self.assertEqual(len(float_violations(line + "\n")), 1)

    def test_violation_inside_a_function_reports_its_line_number(self):
        found = float_violations(
            """
            def charge(amount):
                ok = True
                return amount * 1.029
            """
        )
        self.assertEqual([item["line"] for item in found], [4])

    def test_chained_expression_is_reported_once(self):
        found = float_violations("fee = amount * 0.029 + 0.30\n")
        self.assertEqual(len(found), 1)

    def test_independent_expressions_are_reported_separately(self):
        found = float_violations(
            """
            a = amount * 0.5
            b = fee + 0.1
            """
        )
        self.assertEqual([item["line"] for item in found], [2, 3])

    def test_check_method_accepts_a_parsed_tree(self):
        source = "x = price * 1.5\n"
        guard = HOOK.AegisASTSmiteGuard("sample.py", source)
        guard._check_float_currency_math(ast.parse(source))
        self.assertEqual([item["rule"] for item in guard.violations], ["AEGIS-FLOAT"])


class FloatGuardFalsePositiveTests(unittest.TestCase):
    """Code that must not be flagged."""

    def test_decimal_arithmetic_is_clean(self):
        source = """
            from decimal import Decimal
            fee = amount * Decimal("0.029") + Decimal("0.30")
            net = amount - fee
            """
        self.assertEqual(float_violations(source), [])

    def test_integer_cent_arithmetic_is_clean(self):
        for line in (
            "x = amount_cents / 100",
            "x = price * 3 + fee",
            "total_cents = balance_cents - fee_cents",
        ):
            with self.subTest(statement=line):
                self.assertEqual(float_violations(line + "\n"), [])

    def test_float_math_on_non_monetary_names_is_clean(self):
        for line in (
            "x = timeout * 1.5",
            "x = feedback * 0.5",
            "x = 0.1 + 0.2",
            "x = len(prices) * 1.5",
        ):
            with self.subTest(statement=line):
                self.assertEqual(float_violations(line + "\n"), [])

    def test_float_text_in_comments_and_strings_is_clean(self):
        source = """
            # fee = amount * 0.029
            note = "amount * 0.5"
            """
        self.assertEqual(float_violations(source), [])

    def test_unlisted_operators_are_not_part_of_the_rule(self):
        self.assertEqual(float_violations("x = amount % 0.5\n"), [])

    def test_decimal_arithmetic_stored_into_a_monetary_name_is_clean(self):
        source = """
            from decimal import Decimal
            fee = gross * Decimal("0.029") + Decimal("0.30")
            total_cost: Decimal = base / Decimal("1.07")
            """
        self.assertEqual(float_violations(source), [])

    def test_non_monetary_target_with_float_arithmetic_is_clean(self):
        self.assertEqual(float_violations("ratio = width * 0.75\n"), [])


class ShippedCodeComplianceTests(unittest.TestCase):
    """The repository's own runtime code must satisfy the rule it enforces."""

    def test_shipped_python_has_no_float_currency_math(self):
        scanned = [
            os.path.join(REPO_ROOT, folder)
            for folder in ("src", "apps", "mcp", "scripts")
            if os.path.isdir(os.path.join(REPO_ROOT, folder))
        ]
        self.assertTrue(scanned, "expected at least one source folder to scan")
        result = subprocess.run(
            [sys.executable, HOOK_PATH] + scanned,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
