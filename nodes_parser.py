"""
Parser for BRE/FE nodes.dat files

File format:
Each BBS entry consists of 6 lines followed by a blank line:
1. BBS index (integer)
2. BBS name (string)
3. FidoNet address (string, format: zone:net/node)
4. City (string)
5. State/Province (string)
6. Country (string)
7. Blank line (separator)
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class BBSNode:
    """Represents a single BBS node entry"""
    bbs_index: int
    bbs_name: str
    fidonet_address: str
    city: str
    state: str
    country: str
    line_number: int  # Track where this entry started in the file


class NodesFileParser:
    """Parser for brnodes.dat/fenodes.dat files"""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.nodes: List[BBSNode] = []
        self.errors: List[str] = []

    def parse(self) -> bool:
        """
        Parse the nodes file.
        Returns True if successful, False if errors occurred.
        """
        if not self.file_path.exists():
            self.errors.append(f"File not found: {self.file_path}")
            return False

        try:
            with open(self.file_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception as e:
            self.errors.append(f"Failed to read file: {e}")
            return False

        # Parse entries
        i = 0
        entry_count = 0
        while i < len(lines):
            # Skip blank lines at the start
            while i < len(lines) and lines[i].strip() == '':
                i += 1

            if i >= len(lines):
                break

            # We should have 6 lines for this entry
            start_line = i + 1  # 1-indexed for human readability
            if i + 5 >= len(lines):
                self.errors.append(
                    f"Line {start_line}: Incomplete entry (expected 6 lines, found {len(lines) - i})"
                )
                break

            # Parse the 6 fields
            try:
                bbs_index_str = lines[i].strip()
                bbs_name = lines[i + 1].strip()
                fidonet_address = lines[i + 2].strip()
                city = lines[i + 3].strip()
                state = lines[i + 4].strip()
                country = lines[i + 5].strip()

                # Validate bbs_index is an integer
                try:
                    bbs_index = int(bbs_index_str)
                except ValueError:
                    self.errors.append(
                        f"Line {start_line}: Invalid BBS index '{bbs_index_str}' (must be an integer)"
                    )
                    i += 6
                    continue

                # Validate bbs_index is in valid range
                if not (1 <= bbs_index <= 255):
                    self.errors.append(
                        f"Line {start_line}: BBS index {bbs_index} out of range (must be 1-255)"
                    )

                # Check for required fields
                if not bbs_name:
                    self.errors.append(f"Line {start_line + 1}: BBS name is empty")

                if not fidonet_address:
                    self.errors.append(f"Line {start_line + 2}: FidoNet address is empty")

                # Create node entry
                node = BBSNode(
                    bbs_index=bbs_index,
                    bbs_name=bbs_name,
                    fidonet_address=fidonet_address,
                    city=city,
                    state=state,
                    country=country,
                    line_number=start_line
                )
                self.nodes.append(node)
                entry_count += 1

            except Exception as e:
                self.errors.append(f"Line {start_line}: Parse error: {e}")

            # Move to next entry (skip the 6 lines + blank line separator)
            i += 6
            # Skip blank lines between entries
            while i < len(lines) and lines[i].strip() == '':
                i += 1

        if entry_count == 0 and not self.errors:
            self.errors.append("No entries found in file")
            return False

        return len(self.errors) == 0

    def get_node_by_index(self, bbs_index: int) -> Optional[BBSNode]:
        """Find a node by its BBS index"""
        for node in self.nodes:
            if node.bbs_index == bbs_index:
                return node
        return None

    def get_node_by_name(self, bbs_name: str) -> Optional[BBSNode]:
        """Find a node by its BBS name (case-insensitive)"""
        bbs_name_lower = bbs_name.lower()
        for node in self.nodes:
            if node.bbs_name.lower() == bbs_name_lower:
                return node
        return None

    def check_duplicate_indices(self) -> List[str]:
        """Check for duplicate BBS indices"""
        seen = {}
        duplicates = []
        for node in self.nodes:
            if node.bbs_index in seen:
                duplicates.append(
                    f"Duplicate BBS index {node.bbs_index}: "
                    f"'{seen[node.bbs_index].bbs_name}' (line {seen[node.bbs_index].line_number}) "
                    f"and '{node.bbs_name}' (line {node.line_number})"
                )
            else:
                seen[node.bbs_index] = node
        return duplicates
