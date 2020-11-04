
dev_rm:
	@make -C develop rm

dev_run: 
	@make -C develop run

dev_bash:
	@make -C develop bash

dev_download_kaggle_data:
	@make -C develop download_kaggle_data

dev_build_rawdata:
	@make -C develop build_rawdata

dev_build_dataset:
	@make -C develop build_dataset

dev_train:
	@make -C develop train

dev_generate:
	@make -C develop generate

dev_review:
	@make -C develop review

dev_display_review:
	@make -C develop display_review

svc_push_container:
	@make -C services push_container

svc_install_on_mac:
	@make -C services install_on_mac

svc_run:
	@make -C services run

svc_rm:
	@make -C services rm

svc_pods:
	@make -C services pods

svc_db_bash:
	@make -C services db_bash

svc_dc_bash:
	@make -C services dc_bash

svc_td_bash:
	@make -C services td_bash