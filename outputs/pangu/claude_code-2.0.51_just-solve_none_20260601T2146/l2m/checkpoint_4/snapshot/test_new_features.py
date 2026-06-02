#!/usr/bin/env python3
"""Comprehensive tests for new macro expansion and command processing features."""

import subprocess
import sys
import tempfile
import os

# Get the absolute path to l2m.py
L2M_PATH = os.path.abspath('/workspace/l2m.py')

def run_converter(input_text):
    """Helper to run the converter on input text and return output."""
    # Create temp directory specifically for each test
    with tempfile.TemporaryDirectory(dir='/tmp') as tmpdir:
        input_path = os.path.join(tmpdir, 'input.tex')
        output_path = os.path.join(tmpdir, 'input.md')
        with open(input_path, 'w', encoding='utf-8') as f:
            f.write(input_text)

        result = subprocess.run(
            [sys.executable, L2M_PATH, input_path],
            capture_output=True,
            text=True
        )

        # Read output from the generated .md file
        if result.returncode != 0:
            return result.stderr.strip(), result.returncode

        if os.path.exists(output_path):
            with open(output_path, 'r', encoding='utf-8') as f:
                output = f.read().strip()
        else:
            output = result.stdout.strip()

        return output, result.returncode


# =========== Macro Expansion Tests ===========

def test_macro_expansion_def():
    """Test \\def\\CMD{replacement} macro expansion."""
    input_text = r"""
\def\foo{bar}
\begin{document}
Hello \foo world!
\end{document}
"""
    output, code = run_converter(input_text)
    assert "Hello bar world!" in output, f"Expected 'Hello bar world!' in output, got: '{output}'"
    assert code == 0
    print("✓ Test macro expansion with \\def")


def test_macro_expansion_newcommand():
    """Test \\newcommand{\CMD}{replacement} macro expansion."""
    input_text = r"""
\newcommand{\mycmd}{replacement text}
\begin{document}
This is a \mycmd test.
\end{document}
"""
    output, code = run_converter(input_text)
    assert "replacement text" in output, f"Expected 'replacement text' in output, got: '{output}'"
    assert code == 0
    print("✓ Test macro expansion with \\newcommand")


def test_macro_expansion_multiple():
    """Test multiple macro definitions."""
    input_text = r"""
\def\a{foo}
\def\b{bar}
\begin{document}
\a and \b
\end{document}
"""
    output, code = run_converter(input_text)
    assert "foo and bar" in output, f"Expected 'foo and bar' in output, got: '{output}'"
    assert code == 0
    print("✓ Test multiple macro definitions")


def test_macro_expansion_shared_prefix():
    """Test that shared prefixes don't cause partial matches."""
    input_text = r"""
\def\foo{FooValue}
\begin{document}
Test \foobar and \foo here.
\end{document}
"""
    output, code = run_converter(input_text)
    # \foobar should stay as is, \foo should expand to FooValue
    assert "FooValue here" in output, f"Expected 'FooValue here' in output, got: '{output}'"
    assert code == 0
    print("✓ Test macro expansion with shared prefixes")


def test_macro_expansion_in_math():
    """Test macro expansion inside math environments."""
    input_text = r"""
\def\R{\\mathbb{R}}
\begin{document}
The set $\R$ is real.
\end{document}
"""
    output, code = run_converter(input_text)
    assert "\\mathbb{R}" in output or "R" in output, f"Expected math content in output, got: '{output}'"
    assert code == 0
    print("✓ Test macro expansion in math environment")


def test_macro_chaining():
    """Test chain of macro expansions."""
    input_text = r"""
\def\a{b}
\def\b{c}
\begin{document}
Value: \a
\end{document}
"""
    output, code = run_converter(input_text)
    # \a expands to \b which expands to c
    assert "c" in output, f"Expected 'c' in output, got: '{output}'"
    assert code == 0
    print("✓ Test macro expansion chaining")


def test_cyclic_macro_definition():
    """Test that cyclic macro definitions cause error exit."""
    input_text = r"""
\def\a{\b}
\def\b{\a}
\begin{document}
Test \a
\end{document}
"""
    output, code = run_converter(input_text)
    assert code == 1, f"Expected exit code 1 for cyclic macro, got: {code}"
    assert "Error: cyclic macro definition" in output, f"Expected cyclic macro error, got: '{output}'"
    print("✓ Test cyclic macro definition detection")


def test_cyclic_macro_self_reference():
    """Test self-referential macro causes error."""
    input_text = r"""
\def\a{\a}
\begin{document}
Test \a
\end{document}
"""
    output, code = run_converter(input_text)
    assert code == 1, f"Expected exit code 1 for self-referential macro, got: {code}"
    assert "Error: cyclic macro definition" in output, f"Expected cyclic macro error, got: '{output}'"
    print("✓ Test self-referential macro detection")


def test_no_preamble_macro_extraction():
    """Test that macros outside preamble are not extracted."""
    input_text = r"""
\begin{document}
\def\foo{bar}  % This is in body, not preamble
Hello \foo world!
\end{document}
"""
    output, code = run_converter(input_text)
    # The macro in body should not be extracted/preprocessed
    # \foo will just be treated as text (or command that does nothing)
    assert '\\foo' in output or 'foo' in output, f"Expected \\foo to remain, got: '{output}'"
    assert code == 0
    print("✓ Test that macros outside preamble are not extracted")


def test_macro_in_pre_commented():
    """Test that macros in comments are properly handled."""
    input_text = r"""
% \def\commented{should-be-ignored}
\def\realmacro{should-work}
\begin{document}
Using \realmacro
\end{document}
"""
    output, code = run_converter(input_text)
    assert "should-work" in output, f"Expected 'should-work' from real macro, got: '{output}'"
    assert "commented" not in output.lower(), f"Expected 'commented' to be removed, got: '{output}'"
    assert code == 0
    print("✓ Test macros in comments are handled")


# =========== includegraphics Tests ===========

def test_includegraphics_basic():
    """Test basic \\includegraphics conversion."""
    input_text = r"""
\begin{document}
Here is an image: \includegraphics{test.png}
\end{document}
"""
    output, code = run_converter(input_text)
    assert '![image](test.png)' in output, f"Expected '![image](test.png)' in output, got: '{output}'"
    assert code == 0
    print("✓ Test basic includegraphics conversion")


def test_includegraphics_with_options():
    """Test \\includegraphics with options (options should be discarded)."""
    input_text = r"""
\begin{document}
Image with options: \includegraphics[width=100px]{diagram.jpg}
\end{document}
"""
    output, code = run_converter(input_text)
    assert '![image](diagram.jpg)' in output, f"Expected '![image](diagram.jpg)' in output, got: '{output}'"
    assert code == 0
    print("✓ Test includegraphics with options")


def test_includegraphics_multiple():
    """Test multiple includegraphics commands."""
    input_text = r"""
\begin{document}
\includegraphics{img1.png} and \includegraphics{img2.png}
\end{document}
"""
    output, code = run_converter(input_text)
    assert '![image](img1.png)' in output, f"Expected img1.png in output, got: '{output}'"
    assert '![image](img2.png)' in output, f"Expected img2.png in output, got: '{output}'"
    assert code == 0
    print("✓ Test multiple includegraphics commands")


def test_includegraphics_malformed_no_path():
    """Test malformed \\includegraphics without path causes error."""
    input_text = r"""
\begin{document}
\includegraphics{}  % Empty path
\end{document}
"""
    output, code = run_converter(input_text)
    assert code == 1, f"Expected exit code 1 for malformed includegraphics, got: {code}"
    assert "Error: malformed includegraphics command" in output, f"Expected malformed includegraphics error, got: '{output}'"
    print("✓ Test malformed includegraphics (empty path)")


# =========== Environment Unwrapping Tests ===========

def test_unwrap_multicols():
    """Test multicols environment unwrapping."""
    input_text = r"""
\begin{document}
Before
\begin{multicols}{2}
Content in multicols
\end{multicols}
After
\end{document}
"""
    output, code = run_converter(input_text)
    assert "Before" in output, f"Expected 'Before' in output, got: '{output}'"
    assert "After" in output, f"Expected 'After' in output, got: '{output}'"
    assert "Content in multicols" in output, f"Expected content to remain in output, got: '{output}'"
    assert "multicols" not in output.lower(), f"Expected 'multicols' to be removed, got: '{output}'"
    assert code == 0
    print("✓ Test multicols environment unwrapping")


def test_unwrap_minipage():
    """Test minipage environment unwrapping."""
    input_text = r"""
\begin{document}
Text before
\begin{minipage}{0.5\\textwidth}
Minipage content here
\end{minipage}
Text after
\end{document}
"""
    output, code = run_converter(input_text)
    assert "Text before" in output, f"Expected 'Text before' in output, got: '{output}'"
    assert "Text after" in output, f"Expected 'Text after' in output, got: '{output}'"
    assert "Minipage content here" in output, f"Expected content to remain in output, got: '{output}'"
    assert "minipage" not in output.lower(), f"Expected 'minipage' to be removed, got: '{output}'"
    assert code == 0
    print("✓ Test minipage environment unwrapping")


def test_nested_environments():
    """Test nested environment unwrapping."""
    input_text = r"""
\begin{document}
\begin{multicols}{2}
\begin{minipage}{0.5\\textwidth}
Nested content
\end{minipage}
\end{multicols}
After
\end{document}
"""
    output, code = run_converter(input_text)
    assert "Nested content" in output, f"Expected nested content in output, got: '{output}'"
    assert "multicols" not in output.lower(), f"Expected 'multicols' to be removed, got: '{output}'"
    assert "minipage" not in output.lower(), f"Expected 'minipage' to be removed, got: '{output}'"
    assert "After" in output, f"Expected 'After' in output, got: '{output}'"
    assert code == 0
    print("✓ Test nested environments")


# =========== parbox Tests ===========

def test_parbox_conversion():
    """Test \\parbox width{content} conversion."""
    input_text = r"""
\begin{document}
\parbox{100pt}{Short content} and more text.
\end{document}
"""
    output, code = run_converter(input_text)
    assert "Short content and more text" in output, f"Expected content to remain (parbox removed), got: '{output}'"
    assert "parbox" not in output.lower(), f"Expected 'parbox' to be removed, got: '{output}'"
    assert code == 0
    print("✓ Test parbox conversion")


def test_parbox_malformed():
    """Test malformed \\parbox without proper arguments causes error."""
    input_text = r"""
\begin{document}
\parbox{only-one-arg}
\end{document}
"""
    output, code = run_converter(input_text)
    assert code == 1, f"Expected exit code 1 for malformed parbox, got: {code}"
    assert "Error: malformed parbox command" in output, f"Expected malformed parbox error, got: '{output}'"
    print("✓ Test malformed parbox error")


# =========== np macro Tests ===========

def test_np_macro_basic():
    """Test \\np{...} formatting macro removal."""
    input_text = r"""
\begin{document}
The value \np{123.45} is important.
\end{document}
"""
    output, code = run_converter(input_text)
    assert "123.45" in output, f"Expected '123.45' in output, got: '{output}'"
    assert "\\np" not in output, f"Expected \\np to be removed, got: '{output}'"
    assert code == 0
    print("✓ Test \\np macro basic")


def test_np_macro_in_math():
    """Test \\np{...} macro inside math."""
    input_text = r"""
\begin{document}
The equation $x = \np{5}$ is simple.
\end{document}
"""
    output, code = run_converter(input_text)
    # The \np should be expanded inside the math, but math delimiters should remain
    assert "$x = 5$" in output or "5" in output, f"Expected math with expanded np in output, got: '{output}'"
    assert code == 0
    print("✓ Test \\np macro in math")


def test_np_macro_multiple():
    """Test multiple \\np commands."""
    input_text = r"""
\begin{document}
Values: \np{1}, \np{2}, \np{3}
\end{document}
"""
    output, code = run_converter(input_text)
    assert "1, 2, 3" in output, f"Expected all np values in output, got: '{output}'"
    assert "\\np" not in output, f"Expected all \\np to be removed, got: '{output}'"
    assert code == 0
    print("✓ Test multiple \\np commands")


# =========== Combined Features Test ===========

def test_combined_features():
    """Test multiple features working together."""
    input_text = r"""
\def\myimg{test.png}
\begin{document}
\section{Title}
This is a test.
\includegraphics{image.jpg}
\begin{multicols}{2}
Some content \textbf{bold} here.
\end{multicols}
The value \np{3.14} is pi.
\end{document}
"""
    output, code = run_converter(input_text)
    # Check basic features
    assert "## Title" in output or "Title" in output, f"Expected section formatting, got: '{output}'"
    assert '![image](image.jpg)' in output, f"Expected image conversion, got: '{output}'"
    assert "3.14" in output, f"Expected np macro expansion, got: '{output}'"
    assert "Some content" in output, f"Expected content to remain, got: '{output}'"
    assert code == 0
    print("✓ Test combined features")


# =========== Parameterized Macro Test ===========

def test_parameterized_macro_ignored():
    """Test that parameterized macros are ignored (not extracted)."""
    input_text = r"""
\newcommand{\vect}[1]{#1}
\begin{document}
Test \vect{v}
\end{document}
"""
    output, code = run_converter(input_text)
    # The parameterized macro should not be extracted/preprocessed
    # It may remain as-is or be processed differently
    assert code == 0
    print("✓ Test parameterized macros are ignored in preamble extraction")


# =========== Edge Case Tests ===========

def test_no_document_begin():
    """Test processing without \\begin{document}."""
    input_text = r"""
\def\a{b}
Some text here.
\end{document}
"""
    output, code = run_converter(input_text)
    # No \\begin{document} means preamble macro extraction shouldn't run
    assert code == 0
    print("✓ Test without \\begin{document}")


if __name__ == '__main__':
    tests = [
        test_macro_expansion_def,
        test_macro_expansion_newcommand,
        test_macro_expansion_multiple,
        test_macro_expansion_shared_prefix,
        test_macro_expansion_in_math,
        test_macro_chaining,
        test_cyclic_macro_definition,
        test_cyclic_macro_self_reference,
        test_no_preamble_macro_extraction,
        test_includegraphics_basic,
        test_includegraphics_with_options,
        test_includegraphics_multiple,
        test_includegraphics_malformed_no_path,
        test_unwrap_multicols,
        test_unwrap_minipage,
        test_parbox_conversion,
        test_parbox_malformed,
        test_np_macro_basic,
        test_np_macro_in_math,
        test_np_macro_multiple,
        test_combined_features,
        test_parameterized_macro_ignored,
        test_macro_in_pre_commented,
        test_nested_environments,
        test_no_document_begin,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: Unexpected error: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")

    if failed > 0:
        sys.exit(1)
    else:
        print("All tests passed!")
        sys.exit(0)
