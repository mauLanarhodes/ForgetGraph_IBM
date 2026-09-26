"""
Chunking logic.

split_into_chunks(body) -> list[str]

Algorithm (section 4.1 of architecture.md):
1. Split the body on blank lines to get paragraphs.
2. Pack consecutive paragraphs (joined by a blank line) into chunks of at
   most 600 characters.  A single paragraph that exceeds 600 characters
   becomes its own chunk.
"""

_MAX_CHUNK_SIZE = 600
_SEP = "\n\n"


def split_into_chunks(body: str) -> list[str]:
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for para in paragraphs:
        # Size of this paragraph when appended to the current accumulator
        if current_parts:
            candidate_len = current_len + len(_SEP) + len(para)
        else:
            candidate_len = len(para)

        if current_parts and candidate_len > _MAX_CHUNK_SIZE:
            # Flush the current chunk and start a new one
            chunks.append(_SEP.join(current_parts))
            current_parts = [para]
            current_len = len(para)
        else:
            current_parts.append(para)
            current_len = candidate_len

    if current_parts:
        chunks.append(_SEP.join(current_parts))

    return chunks
