
## Test Admonitions

This is a regular text.

:::tip Definition:

This is a definition of something important.
It can have multiple lines.

:::

\begin{theorem}
If $f$ is continuous, then it has a limit.
\end{theorem}

:::note Exemple:

Consider $x = 2$. Then $x^2 = 4$.

:::

\begin{solution}
We start with the given equation and rearrange terms.
**Step 1**: Add $x$ to both sides.
\end{solution}

\begin{remarque}
This is an important remark.
Inside we can have a nested theorem:
\begin{definition}
Nested definition inside a remark.
\end{definition}
More text after the nested environment.
\end{remarque}

\begin{unknownenv}
This should pass through unchanged.
\begin{theorem}
This nested theorem should NOT be parsed because it's inside an unknown environment.
\end{theorem}
\end{unknownenv}

:::note Preuve:

The proof is straightforward.

:::

## Another Section
Some text after all environments.
