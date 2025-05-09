from dataclasses import dataclass, field
from typing import List, Dict
from pprint import pprint, pformat

@dataclass
class Part:
    brand: str = "Toyota"
    part_number: str = ""
    dimensions: str = ""
    weight: str = ""
    name: str = ""
    price: str = ""
    msrp: str = ""
    savings: str = ""
    url: str = ""
    description: str = ""
    condition: str = ""
    position: str = ""
    other_names: str = ""
    warranty: str = ""
    manufacturer: str = ""
    compatibility: List[str] = field(default_factory=list)
    specifications: Dict[str, str] = field(default_factory=dict)

    # String representation (pretty-printed)
    def __str__(self):
        return pformat(self.__dict__, indent=4, width=80)

    # Getters
    def get_brand(self): return self.brand
    def get_part_number(self): return self.part_number
    def get_dimensions(self): return self.dimensions
    def get_weight(self): return self.weight
    def get_name(self): return self.name
    def get_price(self): return self.price
    def get_msrp(self): return self.msrp
    def get_savings(self): return self.savings
    def get_url(self): return self.url
    def get_description(self): return self.description
    def get_condition(self): return self.condition
    def set_position(self, value): return self.position
    def set_other_names(self, value): return self.other_names
    def set_warranty(self, value): return self.warranty
    def set_manufacturer(self, value): return self.manufacturer
    def get_compatibility(self): return self.compatibility
    def get_specifications(self): return self.specifications

    # Setters
    def set_brand(self, value): self.brand = value
    def set_part_number(self, value): self.part_number = value
    def set_dimensions(self, value): self.dimensions = value
    def set_weight(self, value): self.weight = value
    def set_name(self, value): self.name = value
    def set_price(self, value): self.price = value
    def set_msrp(self, value): self.msrp = value
    def set_savings(self, value): self.savings = value
    def set_url(self, value): self.url = value
    def set_description(self, value): self.description = value
    def set_condition(self, value): self.condition = value
    def set_position(self, value): self.position = value
    def set_other_names(self, value): self.other_names = value
    def set_warranty(self, value): self.warranty = value
    def set_manufacturer(self, value): self.manufacturer = value
    def set_compatibility(self, value: List[str]): self.compatibility = value
    def set_specifications(self, value: Dict[str, str]): self.specifications = value

if __name__ == "__main__":
    #from Part import Part

    part = Part(
        part_number="69040-47060",
        dimensions="10x5x3 inches",
        weight="1.5 lbs",
        name="Front Door Lock Motor LH",
        price="$120.00",
        description="Left hand side door lock actuator with motor",
        compatibility=["Camry 2007", "Camry 2008"],
        specifications={"Voltage": "12V", "Material": "Steel/Plastic"}
    )

    pprint(part.__dict__)

    part.set_part_number("69040-47X60")
    part.set_name("Front Door Lock Motor RH")
    part.set_url("http://hap.com")
    print(part.get_name())
    print(part)