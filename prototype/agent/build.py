"""Copy the shared reader contract into the agent folder (the agent is deployed from agent/ alone).

    python3 -I agent/build.py          (from prototype/)

Commit the copy. tests/test_agentcore_reader.py fails if the two files differ.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "setuhaul", "reader_contract.py")
DST = os.path.join(HERE, "reader_contract.py")

if __name__ == "__main__":
    shutil.copyfile(SRC, DST)
    print("copied", os.path.relpath(SRC, os.path.dirname(HERE)), "->", os.path.relpath(DST, os.path.dirname(HERE)))
