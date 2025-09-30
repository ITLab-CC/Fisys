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
    letzte_aktion: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    typ: Mapped["FilamentTyp"] = relationship("FilamentTyp", back_populates="spulen")

    def get_prozent_voll(self):
        return (self.restmenge / self.gesamtmenge) * 100 if self.gesamtmenge else 0.0


# Tabelle für Filament-Verbrauchs-Logs

class FilamentSpuleHistorie(Base):
    __tablename__ = 'filament_spule_historie'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    spulen_id: Mapped[int] = mapped_column(Integer, nullable=False)
    typ_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    material: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    farbe: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    durchmesser: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    aktion: Mapped[str] = mapped_column(String, nullable=False)
    alt_gewicht: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    neu_gewicht: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    verpackt: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    in_printer: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PrinterJobHistory(Base):
    __tablename__ = 'printer_job_history'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    printer_serial: Mapped[str] = mapped_column(String, nullable=False, index=True)
    printer_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    job_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DashboardNote(Base):
    __tablename__ = 'dashboard_notes'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    author: Mapped['User'] = relationship('User', back_populates='notes')


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
    letzte_aktion: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class FilamentSpuleHistorieRead(BaseModel):
    id: int
    spulen_id: int
    typ_name: Optional[str]
    material: Optional[str]
    farbe: Optional[str]
    durchmesser: Optional[float]
    aktion: str
    alt_gewicht: Optional[float]
    neu_gewicht: Optional[float]
    verpackt: Optional[bool]
    in_printer: Optional[bool]
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DashboardNoteRead(BaseModel):
    id: int
    title: Optional[str]
    message: str
    created_at: datetime
    author_username: Optional[str]
    author_role: Optional[str]
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    discord_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    notes: Mapped[list['DashboardNote']] = relationship('DashboardNote', back_populates='author', cascade="all, delete-orphan")
    discord_notifications: Mapped[list['DiscordNotificationSubscription']] = relationship('DiscordNotificationSubscription', back_populates='user', cascade="all, delete-orphan")

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


class DiscordNotificationSubscription(Base):
    __tablename__ = "discord_notification_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), nullable=False, index=True)
    printer_serial: Mapped[str] = mapped_column(String, nullable=False, index=True)
    job_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default='pending')
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped['User'] = relationship('User', back_populates='discord_notifications')


class DiscordBotConfig(Base):
    __tablename__ = "discord_bot_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    use_dm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    webhook_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bot_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    channel_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    message_template: Mapped[str] = mapped_column(Text, nullable=False, default="Hey {username}, dein Druckauftrag {job_name} auf {printer_name} ist fertig!")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


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
