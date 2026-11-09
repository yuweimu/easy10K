from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LineItem:
    name: str
    item_id: int
    english_name: str
    parent: "LineItem | None" = None


class LineItemChart:
    def __init__(
        self,
        source_path: str | Path | None = None,
    ) -> None:
        if source_path is None:
            source_path = Path(__file__).with_name(
                "line-item-chart.txt"
            )

        self.items: dict[str, LineItem] = {}
        self.item_ids: dict[str, int] = {}
        self._items_by_id: dict[int, LineItem] = {}
        self._load(Path(source_path))

    def _load(self, source_path: Path) -> None:
        if not source_path.is_file():
            raise FileNotFoundError(
                f"Financial statement line-item file not found: {source_path}"
            )

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

                item_id_text, name, english_name = (
                    field.strip() for field in fields[0:3]
                )
                if not item_id_text or not name or not english_name:
                    raise ValueError(
                        f"Invalid line-item definition at {source_path}:"
                        f"{line_number}: {raw_line.rstrip()!r}"
                    )
                try:
                    item_id = int(item_id_text)
                except ValueError:
                    raise ValueError(
                        f"Invalid line-item definition at {source_path}:"
                        f"{line_number}: {raw_line.rstrip()!r}"
                    ) from None
                if name in self.items:
                    raise ValueError(
                        f"Duplicate line-item name at {source_path}:"
                        f"{line_number}: {name!r}"
                    )

                parent_id = item_id // 1000
                parent = self._items_by_id.get(parent_id) if parent_id else None
                item = self._items_by_id.get(item_id)
                if item is None:
                    item = LineItem(
                        name, item_id, english_name, parent
                    )
                    self._items_by_id[item_id] = item
                self.items[name] = item
                self.item_ids[name] = item_id

    def get(self, name: str) -> LineItem | None:
        return self.items.get(name)


    def __getitem__(self, name: str) -> LineItem:
        return self.items[name]

    def get_item_id(self, name: str) -> int:
        try:
            return self.item_ids[name]
        except KeyError:
            raise KeyError(f"Unknown financial statement line item: {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self.items
