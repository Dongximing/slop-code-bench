import sys
sys.path.insert(0, '/workspace')
from l2m import (convert_latex_to_markdown, remove_comments, extract_body,
                 strip_whitespace, convert_display_math, convert_sections_and_formatting,
                 delete_commands)

all_pass = True

# Test 1: remove_comments with escaped %
print("=== Test 1: remove_comments ===")
test = "Normal line % comment\nEscaped backslash percent: \\%% should remain\nLine with % comment at start\nLine % has a comment"
result = remove_comments(test)
expected = "Normal line \nEscaped backslash percent: %\nLine with \nLine "
pass_flag = result == expected
print(f"PASS: {pass_flag}")
if not pass_flag:
    print(f"Expected: {repr(expected)}")
    print(f"Got:      {repr(result)}")
    all_pass = False

# Test 2: extract_body
print("\n=== Test 2: extract_body ===")
test2 = """Before start
\\begin{document}
Here is the body
\\end{document}
After end"""
result2 = extract_body(test2)
expected2 = "\nHere is the body\n"
pass_flag2 = result2 == expected2
print(f"PASS: {pass_flag2}")
if not pass_flag2:
    print(f"Expected: {repr(expected2)}")
    print(f"Got:      {repr(result2)}")
    all_pass = False

# Test 3: strip_whitespace
print("\n=== Test 3: strip_whitespace ===")
result3 = strip_whitespace("  Hello  \n \tWorld\t ")
expected3 = "Hello\nWorld"
pass_flag3 = result3 == expected3
print(f"PASS: {pass_flag3}")
if not pass_flag3:
    print(f"Expected: {repr(expected3)}")
    print(f"Got:      {repr(result3)}")
    all_pass = False

# Test 4: convert_display_math
print("\n=== Test 4: convert_display_math ===")
result4 = convert_display_math(r"Before $$E=mc^2$$ and \[1+2=3\] After")
# Original appends: '\n$$\n' + math_content + '\n$$\n', so trailing newline on After
expected4 = "Before $$E=mc^2$$ and \n$$\n1+2=3\n$$\n After"
pass_flag4 = result4 == expected4
print(f"PASS: {pass_flag4}")
if not pass_flag4:
    print(f"Expected: {repr(expected4)}")
    print(f"Got:      {repr(result4)}")
    all_pass = False

# Test 5: convert_sections
print("\n=== Test 5: convert_sections_and_formatting ===")
result5 = convert_sections_and_formatting(r"\section{First}\subsection{Second}\textbf{bold}\emph{italic}")
expected5 = "## First### Second**bold**_italic_"
pass_flag5 = result5 == expected5
print(f"PASS: {pass_flag5}")
if not pass_flag5:
    print(f"Expected: {repr(expected5)}")
    print(f"Got:      {repr(result5)}")
    all_pass = False

# Test 6: delete_commands
print("\n=== Test 6: delete_commands ===")
result6 = delete_commands(r"Text\vspace{10pt}more\medskip \smallskip \bigskip end")
expected6 = "Textmore   end"
pass_flag6 = result6 == expected6
print(f"PASS: {pass_flag6}")
if not pass_flag6:
    print(f"Expected: {repr(expected6)}")
    print(f"Got:      {repr(result6)}")
    all_pass = False

# Test 7: Full pipeline
print("\n=== Test 7: convert_latex_to_markdown (full pipeline) ===")
result7 = convert_latex_to_markdown(r"\section{Test} % a comment\n\\vspace{5pt}\nSome text")
expected7 = "## Test"
pass_flag7 = result7 == expected7
print(f"PASS: {pass_flag7}")
if not pass_flag7:
    print(f"Expected: {repr(expected7)}")
    print(f"Got:      {repr(result7)}")
    all_pass = False

# Test 8: Unclosed display math
print("\n=== Test 8: unclosed display math ===")
result8 = convert_display_math("Before $$math here no close")
expected8 = "Before $$math here no close"
pass_flag8 = result8 == expected8
print(f"PASS: {pass_flag8}")
if not pass_flag8:
    print(f"Expected: {repr(expected8)}")
    print(f"Got:      {repr(result8)}")
    all_pass = False

# Test 9: Escaped % in comments shouldn't be treated as comments
print("\n=== Test 9: escaped % in remove_comments ===")
test9 = r"Value: \%50 % this is a comment"
result9 = remove_comments(test9)
expected9 = "Value: %50 "
pass_flag9 = result9 == expected9
print(f"PASS: {pass_flag9}")
if not pass_flag9:
    print(f"Expected: {repr(expected9)}")
    print(f"Got:      {repr(result9)}")
    all_pass = False

print("\n" + "=" * 40)
if all_pass:
    print("ALL TESTS PASSED!")
else:
    print("SOME TESTS FAILED!")
    sys.exit(1)