from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FinancialStatementLineItem:
    name: str
    item_id: str
    english_name: str
    parent: "FinancialStatementLineItem | None" = None


class FinancialStatementLineItemDict:
    def __init__(
        self,
        source_path: str | Path | None = None,
    ) -> None:
        if source_path is None:
            source_path = Path(__file__).with_name(
                "financial-statement-line-items.txt"
            )

        self.items: dict[str, FinancialStatementLineItem] = {}
        self.item_ids: dict[str, str] = {}
        self._load(Path(source_path))

    def _load(self, source_path: Path) -> None:
        if not source_path.is_file():
            raise FileNotFoundError(
                f"Financial statement line-item file not found: {source_path}"
            )

        parents: list[tuple[int, FinancialStatementLineItem]] = []
        with source_path.open(encoding="utf-8") as source:
            for line_number, raw_line in enumerate(source, start=1):
                if not raw_line.strip():
                    continue

                content = raw_line.rstrip("\r\n")
                fields = content.split("|")
                if len(fields) != 3:
                    raise ValueError(
                        f"Invalid line-item definition at {source_path}:"
                        f"{line_number}: {raw_line.rstrip()!r}"
                    )

                indentation = content[: len(content) - len(content.lstrip(" \t"))]
                item_id, name, english_name = (
                    field.strip() for field in fields[0:3]
                )
                if not item_id or not name or not english_name:
                    raise ValueError(
                        f"Invalid line-item definition at {source_path}:"
                        f"{line_number}: {raw_line.rstrip()!r}"
                    )
                indent = len(indentation.expandtabs(8))
                if name in self.items:
                    raise ValueError(
                        f"Duplicate line-item name at {source_path}:"
                        f"{line_number}: {name!r}"
                    )

                while parents and parents[-1][0] >= indent:
                    parents.pop()
                parent = parents[-1][1] if parents else None
                item = FinancialStatementLineItem(
                    name, item_id, english_name, parent
                )
                self.items[name] = item
                self.item_ids[name] = item_id
                parents.append((indent, item))

    def get(self, name: str) -> FinancialStatementLineItem | None:
        return self.items.get(name)

    def get_item_id(self, name: str) -> str:
        try:
            return self.item_ids[name]
        except KeyError:
            raise KeyError(f"Unknown financial statement line item: {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self.items

    def __getitem__(self, name: str) -> FinancialStatementLineItem:
        return self.items[name]
