from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.auth import get_current_user
from src.database import get_db
from src.models.post_models import Post as PostModel
from src.posts.crud import create_post, get_post_by_id, get_posts_by_user, update_post, delete_post
from src.schemas.post_schemas import PostCreate, PostUpdate, Post

posts_router = APIRouter(prefix="/posts", tags=["Posts"])


async def get_own_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user)
) -> PostModel:
    """Пост по id: 404, если его нет, и 403, если он принадлежит другому пользователю."""
    post = await get_post_by_id(db, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return post


@posts_router.post("/", response_model=Post)
async def create_new_post(
    post_data: PostCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user)
):
    return await create_post(db, post_data=post_data, user_id=current_user.id)

@posts_router.get("/{post_id}", response_model=Post)
async def read_post(post: PostModel = Depends(get_own_post)):
    return post

@posts_router.get("/", response_model=list[Post])
async def read_user_posts(
    limit: Optional[int] = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user)
):
    return await get_posts_by_user(db, user_id=current_user.id, limit=limit, offset=offset)

@posts_router.put("/{post_id}", response_model=Post)
async def update_existing_post(
    post_data: PostUpdate,
    post: PostModel = Depends(get_own_post),
    db: AsyncSession = Depends(get_db)
):
    return await update_post(db, post=post, post_data=post_data)

@posts_router.delete("/{post_id}")
async def delete_existing_post(
    post: PostModel = Depends(get_own_post),
    db: AsyncSession = Depends(get_db)
):
    await delete_post(db, post=post)
    return {"detail": "Post deleted"}
