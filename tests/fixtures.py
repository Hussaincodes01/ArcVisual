"""A synthetic arXiv e-print, at realistic length.

Real papers are the eval set's job (``eval/papers.yaml``). This fixture exists so
the parser, the gates and the ownership rules can be tested without a network
call — and so a CI run is deterministic.
"""

from __future__ import annotations

import io
import tarfile

from arcvisual.ingest.arxiv import ArxivMetadata

CC_BY = "http://creativecommons.org/licenses/by/4.0/"
ARXIV_DEFAULT = "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"

PAPER_TEX = r"""
\documentclass{article}
\usepackage{amsmath}
\title{Attention Is All You Need}
\author{A. Vaswani \and N. Shazeer}
\begin{document}
\begin{abstract}
The dominant sequence transduction models are based on complex recurrent or
convolutional neural networks that include an encoder and a decoder. We propose a
new simple network architecture, the Transformer, based solely on attention
mechanisms, dispensing with recurrence and convolutions entirely.
\end{abstract}

\section{Introduction}
Recurrent neural networks, long short-term memory and gated recurrent neural
networks in particular, have been firmly established as state of the art
approaches in sequence modeling and transduction problems such as language
modeling and machine translation. Numerous efforts have since continued to push
the boundaries of recurrent language models and encoder-decoder architectures.

Recurrent models typically factor computation along the symbol positions of the
input and output sequences. This inherently sequential nature precludes
parallelization within training examples, which becomes critical at longer
sequence lengths, as memory constraints limit batching across examples. Recent
work has achieved significant improvements in computational efficiency through
factorization tricks and conditional computation, while also improving model
performance in the case of the latter. The fundamental constraint of sequential
computation, however, remains.

Attention mechanisms have become an integral part of compelling sequence
modeling and transduction models in various tasks, allowing modeling of
dependencies without regard to their distance in the input or output sequences.
In all but a few cases, however, such attention mechanisms are used in
conjunction with a recurrent network.

In this work we propose the Transformer, a model architecture eschewing
recurrence and instead relying entirely on an attention mechanism to draw global
dependencies between input and output. The Transformer allows for significantly
more parallelization and can reach a new state of the art in translation quality
after being trained for as little as twelve hours on eight GPUs. This is the
central practical claim of the paper and the one the rest of the work supports.

\section{Background}
The goal of reducing sequential computation also forms the foundation of several
earlier models, all of which use convolutional neural networks as a basic
building block, computing hidden representations in parallel for all input and
output positions. In these models, the number of operations required to relate
signals from two arbitrary input or output positions grows in the distance
between positions, linearly for some architectures and logarithmically for
others. This makes it more difficult to learn dependencies between distant
positions, which is precisely the difficulty attention removes.

Self-attention, sometimes called intra-attention, is an attention mechanism
relating different positions of a single sequence in order to compute a
representation of that sequence. Self-attention has been used successfully in a
variety of tasks including reading comprehension, abstractive summarization,
textual entailment and learning task-independent sentence representations. To
the best of our knowledge, however, the Transformer is the first transduction
model relying entirely on self-attention to compute representations of its input
and output without using sequence-aligned recurrent layers.

\section{Method}
\subsection{Scaled Dot-Product Attention}
We call our particular attention mechanism scaled dot-product attention. The
input consists of queries and keys of dimension $d_k$, and values of dimension
$d_v$. We compute the dot products of the query with all keys, divide each by the
square root of the key dimension, and apply a softmax function to obtain the
weights on the values. In practice we compute the attention function on a set of
queries simultaneously, packed together into a matrix.

The scaling factor is the part of this design that is easy to miss and hard to
do without. For large values of the key dimension, the dot products grow large in
magnitude, pushing the softmax function into regions where it has extremely
small gradients. To counteract this effect, we scale the dot products by the
inverse square root of the key dimension. Without the scaling the model trains
noticeably worse at larger dimensions, which is a claim about optimization rather
than about expressiveness.

\begin{equation}
\label{eq:attn}
\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V
\end{equation}

The two most commonly used attention functions are additive attention and
dot-product attention. Dot-product attention is identical to our algorithm
except for the scaling factor. Additive attention computes the compatibility
function using a feed-forward network with a single hidden layer. While the two
are similar in theoretical complexity, dot-product attention is much faster and
more space-efficient in practice, since it can be implemented using highly
optimized matrix multiplication code.

\subsection{Multi-Head Attention}
Instead of performing a single attention function with keys, values and queries
of the full model dimension, we found it beneficial to linearly project the
queries, keys and values h times with different, learned linear projections. On
each of these projected versions of queries, keys and values we then perform the
attention function in parallel, yielding output values which we concatenate and
once again project, resulting in the final values.

Multi-head attention allows the model to jointly attend to information from
different representation subspaces at different positions. With a single
attention head, averaging inhibits this. The reason the cost stays comparable to
single-head attention is that the dimension of each head is reduced in
proportion to the number of heads, so total computation is roughly unchanged.

\begin{equation}
\mathrm{MultiHead}(Q,K,V) = \mathrm{Concat}(\mathrm{head}_1, \ldots, \mathrm{head}_h) W^O
\end{equation}

\begin{figure}
\includegraphics[width=0.8\textwidth]{figures/architecture.pdf}
\caption{The Transformer model architecture, with the encoder stack on the left
and the decoder stack on the right.}
\label{fig:arch}
\end{figure}

\subsection{Position Encodings}
Since our model contains no recurrence and no convolution, in order for the model
to make use of the order of the sequence, we must inject some information about
the relative or absolute position of the tokens in the sequence. To this end we
add positional encodings to the input embeddings at the bottoms of the encoder
and decoder stacks. The positional encodings have the same dimension as the
embeddings, so that the two can be summed. There are many choices of positional
encodings, learned and fixed, and we found that the two produced nearly
identical results.

\section{Results}
On the WMT 2014 English-to-German translation task, the big Transformer model
outperforms the best previously reported models, including ensembles, by more
than 2.0 BLEU, establishing a new state-of-the-art BLEU score of 28.4. Training
took 3.5 days on eight P100 GPUs, a small fraction of the training cost of the
best models from the literature. On the WMT 2014 English-to-French task, our big
model achieves a BLEU score of 41.0, outperforming all of the previously
published single models at less than a quarter of the training cost.

We also varied the number of attention heads and the attention key and value
dimensions, keeping the amount of computation constant. While single-head
attention is 0.9 BLEU worse than the best setting, quality also drops off with
too many heads. This non-monotonicity is worth dwelling on: more heads is not
uniformly better, and the best configuration is an empirical finding rather than
a consequence of the architecture.

\section{Conclusion}
In this work we presented the Transformer, the first sequence transduction model
based entirely on attention, replacing the recurrent layers most commonly used
in encoder-decoder architectures with multi-headed self-attention. For
translation tasks, the Transformer can be trained significantly faster than
architectures based on recurrent or convolutional layers. We are excited about
the future of attention-based models and plan to apply them to other tasks.
\end{document}
"""


def paper_tarball(tex: str = PAPER_TEX, name: str = "ms.tex") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = tex.encode("utf-8")
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def metadata(license: str = CC_BY, comment: str = "15 pages, 5 figures") -> ArxivMetadata:
    return ArxivMetadata(
        arxiv_id="1706.03762",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        abstract="We propose the Transformer, based solely on attention mechanisms.",
        license=license,
        published="2017-06-12T00:00:00Z",
        comment=comment,
        categories=["cs.CL", "cs.LG"],
    )


ATOM_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models are based on complex
recurrent or convolutional neural networks.</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <arxiv:comment>15 pages, 5 figures</arxiv:comment>
    <link rel="license" href="http://arxiv.org/licenses/nonexclusive-distrib/1.0/"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""
