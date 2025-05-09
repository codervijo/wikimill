from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Iterator
from pprint import pprint, pformat

@dataclass
class Category:
    name: str = "Car"
    brand: str = "Toyota"
    url: str = ""
    description: str = ""
    condition: str = ""
    resources: List[Tuple[str, str]] = field(default_factory=list)  # List of (name, url) tuples

    # String representation (pretty-printed)
    def __str__(self):
        return pformat(self.__dict__, indent=4, width=80)

    # Getters
    def get_name(self): return self.name
    def get_brand(self): return self.brand
    def get_url(self): return self.url
    def get_description(self): return self.description
    def get_condition(self): return self.condition

    # Add a resource (name and URL)
    def add_resource(self, name: str, url: str):
        self.resources.append((name, url))

    def get_part_info(self) -> List[Tuple[str, str]]:
        base = self.url.rstrip("/")
        return [
            (name, f"{base}{url}" if url.startswith("/") else f"{base}/{url}")
            for name, url in self.resources
        ]

    # Setters
    def set_name(self, value): self.name = value
    def set_brand(self, value): self.brand = value
    def set_url(self, value): self.url = value
    def set_description(self, value): self.description = value
    def set_condition(self, value): self.condition = value

if __name__ == "__main__":

    catg = Category(
        name="Front Door Lock Motor LH",
        description="Left hand side door lock actuator with motor",
    )

    pprint(catg.__dict__)

    catg.set_name("Front Door Lock Motor RH")
    catg.set_url("http://hap.com")
        # Add individual parts
    catg.add_resource("Rear Disc Brake Caliper & Dust Cover", "/parts-list/2013-toyota-prius-plug_in/power_train_chassis/rear_disc_brake_caliper_dust_cover.html")
    catg.add_resource("Rear Spring & Shock Absorber", "/parts-list/2013-toyota-prius-plug_in/power_train_chassis/rear_spring_shock_absorber.html")
    catg.add_resource("Shift Lever & Retainer", "/parts-list/2013-toyota-prius-plug_in/power_train_chassis/shift_lever_retainer.html")
    catg.add_resource("Steering Column & Shaft", "/parts-list/2013-toyota-prius-plug_in/power_train_chassis/steering_column_shaft.html")
    catg.add_resource("Steering Wheel", "/parts-list/2013-toyota-prius-plug_in/power_train_chassis/steering_wheel.html")
    print(catg.get_name())
    print(catg)

    for name, url in catg.get_part_info():
        print(f"{name} -> {url}")