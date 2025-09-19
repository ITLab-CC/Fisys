from typing import List, Optional
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Integer, String, Float, Text, ForeignKey, Boolean, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from datetime import datetime
from sqlalchemy.sql import func

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
    printer_serial: Mapped[Optional[str]] = mapped_column(ForeignKey('printers.serial'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    alt_gewicht: Mapped[float] = mapped_column(Float, default=0)
    typ: Mapped["FilamentTyp"] = relationship("FilamentTyp", back_populates="spulen")

    def get_prozent_voll(self):
        return (self.restmenge / self.gesamtmenge) * 100 if self.gesamtmenge else 0.0


# Tabelle für Filament-Verbrauchs-Logs
class FilamentVerbrauch(Base):
    __tablename__ = 'filament_verbrauch'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    typ_id: Mapped[int] = mapped_column(ForeignKey('filament_typ.typ_id'), nullable=False)
    verbrauch_in_g: Mapped[float] = mapped_column(Float, nullable=False)
    datum: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    typ: Mapped["FilamentTyp"] = relationship("FilamentTyp")


# Pydantic model for serializing FilamentSpule
class FilamentSpuleRead(BaseModel):
    spulen_id: int
    typ_id: int               
    gesamtmenge: float
    restmenge: float
    in_printer: bool
    verpackt: bool
    printer_serial: Optional[str] = None
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
    printer_serial: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    rolle: Mapped[str] = mapped_column(String, nullable=False, default="user")

class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    rolle: Mapped[str] = mapped_column(String, nullable=False)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    verwendet: Mapped[bool] = mapped_column(Boolean, default=False)


# Drucker-Konfiguration
class Printer(Base):
    __tablename__ = "printers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    ip: Mapped[str] = mapped_column(String, nullable=False)
    serial: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    access_token: Mapped[str] = mapped_column(String, nullable=False)
    show_on_dashboard: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# Pydantic Schemata für Printer-API
class PrinterCreate(BaseModel):
    name: str
    ip: str
    serial: str
    access_token: str
    show_on_dashboard: bool = True


class PrinterUpdate(BaseModel):
    name: str | None = None
    ip: str | None = None
    serial: str | None = None
    access_token: str | None = None
    show_on_dashboard: bool | None = None


class PrinterRead(BaseModel):
    id: int
    name: str
    ip: str
    serial: str
    access_token: str
    show_on_dashboard: bool
    model_config = ConfigDict(from_attributes=True)
