CONTAINER_NAME=docker ps | grep binance_trader_v2:latest | cut -d ' ' -f 1
CONTAINER_NAMES=docker ps -a | grep binance_trader_v2:latest | cut -d ' ' -f 1

build_container:
	docker build develop/dockerfiles -t binance_trader_v2:latest

rm:
	@docker rm -f $(shell $(CONTAINER_NAMES)) 2> /dev/null || echo

run: rm
	@docker run -d --gpus all --shm-size=16gb -v $(PWD)/develop:/app binance_trader_v2:latest tail -f /dev/null 2> /dev/null || (make rm ; docker run -d -v $(PWD)/develop:/app binance_trader_v2:latest tail -f /dev/null)

run_if_not_exists:
ifeq ($(shell $(CONTAINER_NAME)),)
	make run
endif

bash: run_if_not_exists
	docker exec -it $(shell $(CONTAINER_NAME)) bash

download_kaggle_data: run_if_not_exists
	docker exec -it $(shell $(CONTAINER_NAME)) python -m rawdata_builder.download_kaggle_data $(ARGS)

build_rawdata: run_if_not_exists
	docker exec -it $(shell $(CONTAINER_NAME)) python -m rawdata_builder.build_rawdata $(ARGS)

build_dataset: run_if_not_exists
	docker exec -it $(shell $(CONTAINER_NAME)) python -m dataset_builder.build_dataset_v1 $(ARGS)

train: run_if_not_exists
	docker exec -it $(shell $(CONTAINER_NAME)) python -m trainer.models.predictor_v1 train --mode=train $(ARGS)

generate: run_if_not_exists
	docker exec -it $(shell $(CONTAINER_NAME)) python -m trainer.models.predictor_v1 generate --mode=test $(ARGS)

review: run_if_not_exists
	docker exec -it $(shell $(CONTAINER_NAME)) python -m reviewer.reviewer_v1 run $(ARGS)