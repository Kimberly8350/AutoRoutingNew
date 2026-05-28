"""
Data models for the AutoRouting engine.
"""
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional


@dataclass
class Yard:
    yard: str
    latitude: float
    longitude: float
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""


@dataclass
class Terminal:
    terminal_id: str  # ODBC string, e.g. "T-75-TX-2665"
    terminal_name: str
    latitude: float
    longitude: float
    abbreviation: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    is_diesel_wet: int = 0


@dataclass
class Site:
    site_id: int
    site_name: str
    latitude: float
    longitude: float
    customer_group_name: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    pump_certified: int = 0
    alternate_terminal_ids: list = field(default_factory=list)  # interchangeable terminal IDs


@dataclass
class Driver:
    driver_id: int
    first_name: str
    last_name: str
    yard: str
    board_location: str
    start_time: time
    pump_trained: int = 0
    max_shift_hours: float = 12.0
    # resolved at runtime
    yard_location: Optional[Yard] = None
    terminal_ids: set = field(default_factory=set)  # terminals driver has access to
    restricted_site_ids: set = field(default_factory=set)
    restricted_customer_groups: set = field(default_factory=set)
    # clock events from CE Connect (populated by data loader)
    route_start_time: Optional[datetime] = None
    route_finish_time: Optional[datetime] = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def shift_start_dt(self) -> Optional[datetime]:
        return None  # set externally with dispatch_date


@dataclass
class LoadProduct:
    product_name: str
    gross_gallons: float


@dataclass
class Load:
    ce_id: int
    delivery_date: str
    customer_name: str
    order_number: Optional[str]
    site_id: int
    terminal_id: str  # ODBC string, e.g. "T-75-TX-2665"
    terminal_name: str
    products: list[LoadProduct]
    load_status: int
    city: str = ""
    state: str = ""
    site_name: str = ""
    site_address: str = ""
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    delivery_eta: Optional[datetime] = None
    completed_delivery_time: Optional[datetime] = None
    assigned_driver_id: Optional[int] = None
    assigned_driver_first: Optional[str] = None
    assigned_driver_last: Optional[str] = None
    # resolved
    site: Optional[Site] = None
    terminal: Optional[Terminal] = None

    @property
    def is_anytime(self) -> bool:
        if self.window_start is None or self.window_end is None:
            return True
        return self.window_start.hour == 0 and self.window_start.minute == 0 \
               and self.window_end.hour == 23

    @property
    def product_names(self) -> list[str]:
        return [p.product_name for p in self.products]

    @property
    def has_gasoline(self) -> bool:
        gas = {"Regular", "MidGrade", "Super", "Gas-Other"}
        return any(p.product_name in gas for p in self.products)

    @property
    def has_diesel(self) -> bool:
        return any("diesel" in p.product_name.lower() for p in self.products)

    @property
    def has_dyed(self) -> bool:
        return any("dyed" in p.product_name.lower() for p in self.products)

    @property
    def has_bio(self) -> bool:
        return any("bio" in p.product_name.lower() for p in self.products)


@dataclass
class RouteStop:
    ce_id: int
    sequence: int
    terminal: Terminal
    site: Site
    depart_yard: Optional[datetime] = None
    arrive_terminal: Optional[datetime] = None
    depart_terminal: Optional[datetime] = None
    arrive_site: Optional[datetime] = None
    depart_site: Optional[datetime] = None
    drive_to_terminal_mins: float = 0.0
    drive_to_site_mins: float = 0.0
    loaded_miles: float = 0.0
    empty_miles: float = 0.0
    wait_mins: float = 0.0  # waiting for delivery window


@dataclass
class DriverRoute:
    driver: Driver
    stops: list[RouteStop] = field(default_factory=list)
    return_to_yard_time: Optional[datetime] = None
    total_loaded_miles: float = 0.0
    total_empty_miles: float = 0.0
    total_shift_mins: float = 0.0


@dataclass
class AssignmentResult:
    success: bool
    route: Optional[DriverRoute] = None
    failure_reason: Optional[str] = None
    failure_category: Optional[str] = None


@dataclass
class DispatchResult:
    dispatch_date: str
    run_type: str  # 'dispatch' or 'reroute'
    run_id: str
    driver_routes: list[DriverRoute]
    unassigned: list[tuple]  # (load, reason, category)
    total_loads: int = 0
    assigned_loads: int = 0
    unassigned_loads: int = 0
    run_duration_ms: int = 0
