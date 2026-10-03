from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.clock import utcnow
from src.models.post_models import Post
from src.schemas.post_schemas import PostCreate, PostUpdate


async def create_post(db: AsyncSession, post_data: PostCreate, user_id: int):
    new_post = Post(
        user_id=user_id,
        title=post_data.title,
        content=post_data.content
    )
    db.add(new_post)
    await db.commit()
    await db.refresh(new_post)
    return new_post


async def get_post_by_id(db: AsyncSession, post_id: int):
    result = await db.execute(select(Post).where(Post.id == post_id))
    return result.scalars().first()

async def get_posts_by_user(db: AsyncSession, user_id: int, limit: Optional[int] = None, offset: int = 0):
    query = (
        select(Post)
        .where(Post.user_id == user_id)
        .order_by(Post.created_at.desc(), Post.id.desc())
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    return result.scalars().all()

async def update_post(db: AsyncSession, post: Post, post_data: PostUpdate):
    # Обновляем только переданные поля
    if post_data.title is not None:
        post.title = post_data.title
    if post_data.content is not None:
        post.content = post_data.content

    post.updated_at = utcnow()
    await db.commit()
    await db.refresh(post)
    return post

async def delete_post(db: AsyncSession, post: Post):
    await db.delete(post)
    await db.commit()
