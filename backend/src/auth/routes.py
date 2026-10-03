from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.auth.auth import authenticate_user, create_access_token, get_current_active_user, get_password_hash
from src.database import get_db
from src.models.avatars import AvatarDB
from src.models.models import UserDB
from src.schemas.schemas import RegisterResponse, Token, User, UserCreate

auth_router = APIRouter(
    tags=["Authentication"],
    prefix="/auth"
)

AVATAR_URL = "/users/avatar"


@auth_router.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}


# Пути со слэшем на конце оставлены для совместимости и скрыты из документации
@auth_router.get("/users/me", response_model=User)
@auth_router.get("/users/me/", response_model=User, include_in_schema=False)
async def read_users_me(
    current_user: UserDB = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    has_avatar = await db.execute(select(AvatarDB.id).where(AvatarDB.user_id == current_user.id))
    return User(
        id=current_user.id,
        username=current_user.username,
        avatar_url=AVATAR_URL if has_avatar.first() else None
    )


@auth_router.post("/register", response_model=RegisterResponse)
@auth_router.post("/register/", response_model=RegisterResponse, include_in_schema=False)
async def register(user: UserCreate, db: AsyncSession = Depends(get_db)):
    existing_user = await db.execute(
        select(UserDB.id).where(func.lower(UserDB.username) == user.username.lower()))
    if existing_user.first():
        raise HTTPException(status_code=400, detail="Username already registered")
    hashed_password = await run_in_threadpool(get_password_hash, user.password)
    db.add(UserDB(username=user.username, hashed_password=hashed_password))
    try:
        await db.commit()
    except IntegrityError:
        # два одновременных запроса с одним логином
        await db.rollback()
        raise HTTPException(status_code=400, detail="Username already registered")
    access_token = create_access_token(data={"sub": user.username})

    return {
        "message": "User registered successfully",
        "access_token": access_token,
        "token_type": "bearer"
    }
