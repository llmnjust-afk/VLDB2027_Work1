#!/usr/bin/env python3
"""Build the hash-pinned Star kernels with one resident maintenance index."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f"Expected one Star source occurrence of {old!r}")
    return source.replace(old, new, 1)


def checked_sources(star_root):
    for row in (ROOT / "scripts/star-source.sha256").read_text().splitlines():
        expected, relative = row.split()
        source = star_root / relative
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Unexpected Star release source: {source}")
    return star_root / "code"


def build(code, output, compiler):
    output.mkdir(parents=True, exist_ok=True)
    generated = output / "star-source"
    generated.mkdir(exist_ok=True)

    def read(relative):
        return (code / relative).read_text(encoding="latin1")

    def write(name, body):
        (generated / name).write_text(body, encoding="latin1")

    # Start from the release insertion layout. Add deletion metadata, and
    # share the ordered adjacency and triangle mates with the deletion kernel.
    layout = read("ballsIns/myG.h")
    layout = replace_once(layout, "    int iSeSup;\n",
                          "    int iSeSup;\n    int iKSup;\n    int iKMSup;\n"
                          "    int iOldL;\n    bool bDgdFlag;\n    bool bRmFlag;\n"
                          "    bool bDelVstFlag;\n    bool bDelCanQFlag;\n"
                          "    bool resident_dirty;\n    bool resident_recount;\n")
    layout = replace_once(layout, "    int add(int x, int y);",
                          "    int add(int x, int y);\n    int rm(int eid);\n"
                          "    void record_insertion_change(TPST_MAP_BY_EID* edge);\n"
                          "    vector<int> resident_changes;")
    layout = replace_once(layout, "class myG",
                          "struct AdjEntry { int pid; int eid; };\n\nclass myG")
    write("myG.h", "#pragma once\n" + layout)

    # Include the release implementation with the common layout above.
    write("myG.cpp", read("ballsIns/myG.cpp"))
    # Retain checks, and send fatal reports from the release to stderr.
    common = read("ballsIns/common.h").replace("#define DEBUG_PRINT_BTN\n", "")
    write("common.h", "#pragma once\n" + common + "\n#include <climits>\n")

    insertion = read("ballsIns/insertPart.cpp")
    # Only assignments to per-edge trussness/layer can change deletion
    # support on already-present triangles. Track them at the assignment.
    insertion, changed = re.subn(
        r"^( +)(pst(?!PInfo)\w+)->(iTrussness|iLayer) = (.+);$",
        r"\1mpG.record_insertion_change(\2);\n\1\2->\3 = \4;", insertion,
        flags=re.MULTILINE,
    )
    if changed != 19:
        raise ValueError(f"Unexpected Star insertion change sites: {changed}")
    # An earlier star may have raised k within the current insertion call.
    # Also keep slot 2 present when the inserted edge has no triangle.
    insertion = replace_once(
        insertion, "        vBin[iTriT].push_back",
        "        if (static_cast<size_t>(iTriT) >= vBin.size()) vBin.resize(iTriT + 1);\n"
        "        vBin[iTriT].push_back")
    insertion = replace_once(insertion, "    vBin.resize(iMaxK + 1);",
                             "    vBin.resize(std::max(iMaxK, 2) + 1);")
    write("insertPart.cpp", insertion)
    write("insertPart.h", read("ballsIns/insertPart.h"))

    for filename in ("delPart.cpp", "delPart.h"):
        deletion = read("ballDel/" + filename)
        deletion = deletion.replace("vector <AdjEntry> &vNeibE", "vector <TPST_ADJ> &vNeibE")
        deletion = re.sub(r"vNeibE\[([^\n]+?)\]\.(eid|pid)\b",
                          lambda m: f"vNeibE[{m[1]}].i{m[2].capitalize()}", deletion)
        # The release uses unsigned mates in delete and signed mates
        # in insert. The shared index uses signed Star edge IDs.
        deletion = re.sub(r"vector\s*<uint32_t>", "vector<int>", deletion)
        deletion = deletion.replace("bVstFlag", "bDelVstFlag").replace("bCanQFlag", "bDelCanQFlag")
        deletion = deletion.replace("TPST_ORDER", "TPST_DELETE_ORDER").replace("tpstOrder", "tpstDeleteOrder")
        deletion = deletion.replace("g_lBFSCnt", "g_lDeleteBFSCnt")
        if filename.endswith(".cpp"):
            deletion = replace_once(deletion, "    pstNode->vLfE.clear();\n    pstNode->vLfE.clear();",
                                    "    pstNode->vLfE.clear();\n    pstNode->vRtE.clear();")
        write(filename, deletion)

    # Decomposition and layer initialization are the released algorithm.
    # Accept an already-loaded graph and export its annotations in memory.
    decomp_h = read("index/decomp.h")
    decomp_h = replace_once(decomp_h, "  Decomp(const std::string& file_name);",
                           "  Decomp(std::uint32_t nodes, "
                           "std::vector<std::pair<std::uint32_t, std::uint32_t>> edges);\n"
                           "  const auto& edges() const { return edges_; }\n"
                           "  const auto& trussness() const { return m_t; }\n"
                           "  const auto& layer() const { return m_L; }\n"
                           "  const auto& layer_support() const { return m_SeSup; }\n"
                           "  std::uint32_t max_degree() const { return m_dMax; }\n"
                           "  std::uint32_t max_trussness() const { return m_kMax; }")
    write("decomp.h", decomp_h)
    decomp_source = read("index/decomp.cc")
    start = decomp_source.index("Decomp::Decomp(")
    end = decomp_source.index("  std::sort(edges_", start)
    decomp_source = (
        decomp_source[:start]
        + "Decomp::Decomp(uint32_t nodes, std::vector<std::pair<uint32_t, uint32_t>> edges)\n"
          "    : n_(nodes), m_(edges.size()), m_dMax(0), m_kMax(0), edges_(std::move(edges))\n"
          "{\n"
          "  if (edges_.empty()) return;\n"
        + decomp_source[end:]
    )
    decomp_source = re.sub(r"^  const auto (begCnt|endCnt|difCnt|end|dif) = .*;\n",
                           "", decomp_source, flags=re.MULTILINE)
    decomp_source = re.sub(r'^  printf\("(Counting|Indexing) costs .*\n[^\n]+\n',
                           "", decomp_source, flags=re.MULTILINE)
    write("decomp.cc", decomp_source)

    # Existing release assertions terminate with status zero; report failures
    # as such in the benchmark while preserving all assertion checks.
    debug = read("ballsIns/tool/debug.h")
    debug = debug.replace('printf("file:', 'fprintf(stderr, "file:')
    debug = debug.replace('printf("backtrace', 'fprintf(stderr, "backtrace')
    debug = debug.replace("STDOUT_FILENO", "STDERR_FILENO").replace("exit(0)", "exit(EXIT_FAILURE)")
    (generated / "tool").mkdir(exist_ok=True)
    write("tool/debug.h", debug)

    flags = ["-O3", "-march=native", "-std=c++17", "-pthread"]
    command = [compiler, *flags, "-I", str(generated), "-I", str(code / "ballsIns"),
               "-I", str(ROOT / "src"), str(ROOT / "src/star_resident.cpp"),
               *(str(generated / name) for name in ("myG.cpp", "delPart.cpp", "insertPart.cpp", "decomp.cc")),
               "-o", str(output / "star-resident")]
    with (output / "star-build.log").open("w") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--star-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    args = parser.parse_args()
    build(checked_sources(args.star_root.resolve()), args.output.resolve(), args.cxx)


if __name__ == "__main__":
    main()
