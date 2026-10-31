from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from src.quantcore.universe.models import Board, SecurityType

SSE_T1_SOURCE = (
    "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988782.shtml"
)
SSE_STAR_QUANTITY_SOURCE = "https://edu.sse.com.cn/tib/ysptj/c/4768085.shtml"
STAMP_TAX_SOURCE = "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html"


class UnsupportedRuleError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuleVersion:
    rule_id: str
    effective_from: date
    effective_to: date | None
    boards: frozenset[Board]
    security_types: frozenset[SecurityType]
    buy_minimum: int
    buy_increment: int
    settlement_sessions: int
    stamp_tax_sell: Decimal
    commission_rate: Decimal
    commission_minimum: Decimal
    sources: tuple[str, ...]

    def active(self, on: date) -> bool:
        return self.effective_from <= on and (
            self.effective_to is None or on < self.effective_to
        )

    def validate_buy(self, shares: int) -> None:
        if (
            shares < self.buy_minimum
            or (shares - self.buy_minimum) % self.buy_increment
        ):
            raise ValueError("buy quantity violates historical rule")

    def fees(self, side: str, gross: Decimal) -> Decimal:
        commission = max(
            (gross * self.commission_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            self.commission_minimum,
        )
        stamp = (
            (gross * self.stamp_tax_sell).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if side == "SELL"
            else Decimal("0")
        )
        return commission + stamp


class RuleBook:
    def __init__(self, versions: list[RuleVersion]):
        self.versions = tuple(versions)

    def resolve(
        self, on: date, board: Board, security_type: SecurityType
    ) -> RuleVersion:
        matches = [
            x
            for x in self.versions
            if x.active(on) and board in x.boards and security_type in x.security_types
        ]
        if len(matches) != 1:
            raise UnsupportedRuleError(
                f"no unique supported rule for {on}/{board}/{security_type}"
            )
        return matches[0]


def default_rulebook() -> RuleBook:
    stock_types = frozenset({SecurityType.STOCK})
    main = frozenset({Board.SSE_MAIN, Board.SZSE_MAIN, Board.CHINEXT})
    src = (
        SSE_T1_SOURCE,
        STAMP_TAX_SOURCE,
        "RESEARCH_ASSUMPTION:commission=0.025%,minimum=5CNY",
    )
    star_src = (
        SSE_T1_SOURCE,
        SSE_STAR_QUANTITY_SOURCE,
        STAMP_TAX_SOURCE,
        "RESEARCH_ASSUMPTION:commission=0.025%,minimum=5CNY",
    )
    etf_boards = frozenset({Board.SSE_MAIN, Board.SZSE_MAIN})
    etf_source = (
        "RESEARCH_ASSUMPTION:classified ETF settlement must be verified by security master",
        "RESEARCH_ASSUMPTION:ETF buy lot=100,no stamp tax,commission=0.025%,minimum=5CNY",
    )
    etf_rules = [
        RuleVersion(
            "domestic-equity-etf-v1",
            date(2018, 1, 1),
            None,
            etf_boards,
            frozenset({SecurityType.ETF_DOMESTIC_EQUITY}),
            100,
            100,
            1,
            Decimal("0"),
            Decimal("0.00025"),
            Decimal("5.00"),
            etf_source,
        ),
        RuleVersion(
            "t0-etf-v1",
            date(2018, 1, 1),
            None,
            etf_boards,
            frozenset(
                {
                    SecurityType.ETF_BOND,
                    SecurityType.ETF_GOLD_COMMODITY,
                    SecurityType.ETF_CROSS_BORDER,
                    SecurityType.ETF_MONEY,
                }
            ),
            100,
            100,
            0,
            Decimal("0"),
            Decimal("0.00025"),
            Decimal("5.00"),
            etf_source,
        ),
    ]
    return RuleBook(
        [
            RuleVersion(
                "ashare-main-pre-20230828",
                date(2018, 1, 1),
                date(2023, 8, 28),
                main,
                stock_types,
                buy_minimum=100,
                buy_increment=100,
                settlement_sessions=1,
                stamp_tax_sell=Decimal("0.001"),
                commission_rate=Decimal("0.00025"),
                commission_minimum=Decimal("5.00"),
                sources=src,
            ),
            RuleVersion(
                "ashare-main-post-20230828",
                date(2023, 8, 28),
                None,
                main,
                stock_types,
                buy_minimum=100,
                buy_increment=100,
                settlement_sessions=1,
                stamp_tax_sell=Decimal("0.0005"),
                commission_rate=Decimal("0.00025"),
                commission_minimum=Decimal("5.00"),
                sources=src,
            ),
            RuleVersion(
                "star-pre-20230828",
                date(2019, 7, 22),
                date(2023, 8, 28),
                frozenset({Board.STAR}),
                stock_types,
                buy_minimum=200,
                buy_increment=1,
                settlement_sessions=1,
                stamp_tax_sell=Decimal("0.001"),
                commission_rate=Decimal("0.00025"),
                commission_minimum=Decimal("5.00"),
                sources=star_src,
            ),
            RuleVersion(
                "star-post-20230828",
                date(2023, 8, 28),
                None,
                frozenset({Board.STAR}),
                stock_types,
                buy_minimum=200,
                buy_increment=1,
                settlement_sessions=1,
                stamp_tax_sell=Decimal("0.0005"),
                commission_rate=Decimal("0.00025"),
                commission_minimum=Decimal("5.00"),
                sources=star_src,
            ),
            RuleVersion(
                "bse-stock-v1",
                date(2021, 11, 15),
                None,
                frozenset({Board.BSE}),
                stock_types,
                buy_minimum=100,
                buy_increment=1,
                settlement_sessions=1,
                stamp_tax_sell=Decimal("0.0005"),
                commission_rate=Decimal("0.00025"),
                commission_minimum=Decimal("5.00"),
                sources=src,
            ),
            *etf_rules,
        ]
    )
