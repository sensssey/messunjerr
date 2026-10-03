from sqlalchemy import Column, ForeignKey, Integer, Text

from src.database import Base


class AvatarDB(Base):
    __tablename__ = "avatars"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    file_data = Column(Text)
