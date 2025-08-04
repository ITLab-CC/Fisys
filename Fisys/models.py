from typing import List, Optional
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Integer, String, Float, Text, ForeignKey, Boolean, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from datetime import datetime

class Base(DeclarativeBase):
    pass

class FilamentTyp(Base):
    __tablename__ = 'filament_typ'

    id: Mapped[int] = mapped_column('typ_id', Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    material: Mapped[str] = mapped_column(String, nullable=False)
    farbe: Mapped[str] = mapped_column(String, nullable=False)
    durchmesser: Mapped[float] = mapped_column(Float, nullable=False)
    hersteller: Mapped[Optional[str]] = mapped_column(String)
    hinweise: Mapped[Optional[str]] = mapped_column(Text)
    bildname: Mapped[Optional[str]] = mapped_column(String, default="platzhalter.jpg")
    leergewicht: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    spulen: Mapped[List["FilamentSpule"]] = relationship("FilamentSpule", back_populates="typ", cascade="all, delete-orphan")

class FilamentSpule(Base):
    __tablename__ = 'filament_spule'

    spulen_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    typ_id: Mapped[int] = mapped_column(ForeignKey('filament_typ.typ_id'), nullable=False)
    gesamtmenge: Mapped[float] = mapped_column(Float, nullable=False)
    restmenge: Mapped[float] = mapped_column(Float, nullable=False)
    in_printer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verpackt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    typ: Mapped["FilamentTyp"] = relationship("FilamentTyp", back_populates="spulen")

    def get_prozent_voll(self):
        return (self.restmenge / self.gesamtmenge) * 100 if self.gesamtmenge else 0.0


# Pydantic model for serializing FilamentSpule
class FilamentSpuleRead(BaseModel):
    spulen_id: int
    typ_id: int               
    gesamtmenge: float
    restmenge: float
    in_printer: bool
    verpackt: bool
    model_config = ConfigDict(from_attributes=True)

class FilamentSpuleCreate(BaseModel):
    name: str
    material: str
    farbe: str
    durchmesser: float
    leergewicht: float
    hersteller: Optional[str] = None
    gesamtmenge: Optional[float] = None
    restmenge: Optional[float] = None
    in_printer: Optional[bool] = False
    verpackt: Optional[bool] = False

    model_config = ConfigDict(from_attributes=True)